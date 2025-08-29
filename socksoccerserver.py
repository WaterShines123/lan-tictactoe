# server.py
# Sock Soccer — authoritative websocket server
# Dependencies: pip install websockets
# Run: python server.py  (default ws://localhost:8765)

import asyncio
import json
import math
import os
import random
import time
import websockets
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

TICK_RATE = 30  # server ticks per second
DT = 1.0 / TICK_RATE

FIELD_W, FIELD_H = 1200, 700
GOAL_W, GOAL_H = 20, 220
GOAL_LEFT_X = 0
GOAL_RIGHT_X = FIELD_W - GOAL_W

BALL_R = 14
PLAYER_R = 18

MAX_PLAYERS = 4
WIN_GOALS = 5
ROUND_SECONDS = 180

PLAYER_SPEED = 320.0
SPRINT_MULT = 1.35
PLAYER_FRICTION = 0.86
BALL_FRICTION = 0.985
KICK_COOLDOWN = 0.6
KICK_IMPULSE = 540.0
STAMINA_MAX = 100.0
STAMINA_DRAIN = 60.0   # per second
STAMINA_REGEN = 40.0   # per second

TEAM_RED = 0
TEAM_BLUE = 1

def clamp(v,a,b): return max(a, min(b, v))

@dataclass
class Player:
    pid: str
    name: str
    team: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    sprint: bool = False
    kick: bool = False
    aimx: float = 1.0
    aimy: float = 0.0
    last_input_ts: float = 0.0
    cooldown: float = 0.0
    stamina: float = STAMINA_MAX
    score: int = 0
    connected: bool = True

@dataclass
class Ball:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0

@dataclass
class Game:
    players: Dict[str, Player] = field(default_factory=dict)
    sockets: Dict[str, websockets.WebSocketServerProtocol] = field(default_factory=dict)
    ball: Ball = field(default_factory=lambda: Ball(x=FIELD_W/2, y=FIELD_H/2))
    score_red: int = 0
    score_blue: int = 0
    round_end_ts: float = field(default_factory=lambda: time.time() + ROUND_SECONDS)
    last_event: str = ""  # brief toast text
    last_event_t: float = 0.0

    async def broadcast(self, msg: dict):
        if not self.sockets: return
        data = json.dumps(msg)
        await asyncio.gather(*[ws.send(data) for ws in list(self.sockets.values()) if ws])

    def reset_positions(self, after_goal: bool = True):
        # players spawn in halves
        red_slots = [(FIELD_W*0.25, FIELD_H*0.4), (FIELD_W*0.25, FIELD_H*0.6)]
        blue_slots = [(FIELD_W*0.75, FIELD_H*0.4), (FIELD_W*0.75, FIELD_H*0.6)]
        r_i = b_i = 0
        for p in self.players.values():
            if p.team == TEAM_RED:
                sx, sy = red_slots[r_i % len(red_slots)]; r_i += 1
            else:
                sx, sy = blue_slots[b_i % len(blue_slots)]; b_i += 1
            p.x, p.y = sx + random.uniform(-20, 20), sy + random.uniform(-20, 20)
            p.vx = p.vy = 0.0
            p.cooldown = 0.0
        self.ball.x, self.ball.y = FIELD_W/2, FIELD_H/2
        self.ball.vx = self.ball.vy = 0.0
        if after_goal:
            self.last_event_t = time.time()

    def team_counts(self):
        r = sum(1 for p in self.players.values() if p.team == TEAM_RED)
        b = sum(1 for p in self.players.values() if p.team == TEAM_BLUE)
        return r, b

    def assign_team(self):
        r, b = self.team_counts()
        return TEAM_RED if r <= b else TEAM_BLUE

    def add_player(self, pid: str, name: str):
        team = self.assign_team()
        p = Player(
            pid=pid, name=name[:12] or "Player", team=team,
            x=(FIELD_W*0.25 if team==TEAM_RED else FIELD_W*0.75),
            y=FIELD_H/2
        )
        self.players[pid] = p

    def remove_player(self, pid: str):
        if pid in self.players:
            del self.players[pid]
        if pid in self.sockets:
            del self.sockets[pid]

    def physics_player(self, p: Player, dt: float):
        # movement from input is applied in handle_input; here, friction + bounds
        p.vx *= PLAYER_FRICTION
        p.vy *= PLAYER_FRICTION
        p.x += p.vx * dt
        p.y += p.vy * dt
        # walls
        if p.x < PLAYER_R: p.x = PLAYER_R; p.vx = 0
        if p.x > FIELD_W-PLAYER_R: p.x = FIELD_W-PLAYER_R; p.vx = 0
        if p.y < PLAYER_R: p.y = PLAYER_R; p.vy = 0
        if p.y > FIELD_H-PLAYER_R: p.y = FIELD_H-PLAYER_R; p.vy = 0

    def physics_ball(self, dt: float):
        b = self.ball
        b.x += b.vx * dt
        b.y += b.vy * dt
        b.vx *= BALL_FRICTION
        b.vy *= BALL_FRICTION

        # walls (except goals openings)
        # Left and right walls have goal gaps vertically centered
        goal_top = (FIELD_H - GOAL_H) / 2
        goal_bottom = goal_top + GOAL_H

        # left wall
        if b.x - BALL_R < 0:
            if goal_top < b.y < goal_bottom:
                # goal for Blue (right team scores on left goal)
                self.score_goal(scoring_team=TEAM_BLUE)
            else:
                b.x = BALL_R; b.vx = abs(b.vx) * 0.7
        # right wall
        if b.x + BALL_R > FIELD_W:
            if goal_top < b.y < goal_bottom:
                # goal for Red
                self.score_goal(scoring_team=TEAM_RED)
            else:
                b.x = FIELD_W - BALL_R; b.vx = -abs(b.vx) * 0.7
        # top/bottom
        if b.y - BALL_R < 0:
            b.y = BALL_R; b.vy = abs(b.vy) * 0.7
        if b.y + BALL_R > FIELD_H:
            b.y = FIELD_H - BALL_R; b.vy = -abs(b.vy) * 0.7

        # collisions with players
        for p in self.players.values():
            dx = b.x - p.x
            dy = b.y - p.y
            dist2 = dx*dx + dy*dy
            rad = BALL_R + PLAYER_R
            if dist2 < rad*rad:
                dist = math.sqrt(dist2) if dist2 > 0 else 0.0001
                nx, ny = dx / dist, dy / dist
                # separate
                overlap = rad - dist
                b.x += nx * overlap * 0.6
                b.y += ny * overlap * 0.6
                # transfer velocity (simple impulse)
                rel_vx = b.vx - p.vx
                rel_vy = b.vy - p.vy
                dot = rel_vx*nx + rel_vy*ny
                if dot < 0:
                    b.vx -= nx * dot
                    b.vy -= ny * dot
                # small push from player's movement
                b.vx += p.vx * 0.25
                b.vy += p.vy * 0.25

        # clamp tiny velocities
        if abs(b.vx) < 4: b.vx = 0
        if abs(b.vy) < 4: b.vy = 0

    def score_goal(self, scoring_team: int):
        if scoring_team == TEAM_RED:
            self.score_red += 1
            self.last_event = "GOAL! Red +1"
        else:
            self.score_blue += 1
            self.last_event = "GOAL! Blue +1"
        # center kick-off
        self.reset_positions(after_goal=True)

    def handle_input(self, p: Player, inp: dict, dt: float):
        up = inp.get("up", False)
        down = inp.get("down", False)
        left = inp.get("left", False)
        right = inp.get("right", False)
        sprint = inp.get("sprint", False)
        kick = inp.get("kick", False)
        aimx = float(inp.get("aimx", 1.0))
        aimy = float(inp.get("aimy", 0.0))

        p.aimx, p.aimy = aimx, aimy

        speed = PLAYER_SPEED * (SPRINT_MULT if sprint and p.stamina > 0 else 1.0)
        ax = (right - left) * speed
        ay = (down - up) * speed
        p.vx += ax * dt
        p.vy += ay * dt

        # stamina
        if sprint and (abs(ax) > 0 or abs(ay) > 0):
            p.stamina -= STAMINA_DRAIN * dt
        else:
            p.stamina += STAMINA_REGEN * dt
        p.stamina = clamp(p.stamina, 0, STAMINA_MAX)

        # kicking
        if kick and p.cooldown <= 0.0:
            # if close to ball, apply impulse along aim
            dx = self.ball.x - p.x
            dy = self.ball.y - p.y
            if dx*dx + dy*dy <= (PLAYER_R + BALL_R + 6)**2:
                # normalize aim or use to ball direction if aim nearly zero
                mag = math.hypot(aimx, aimy)
                if mag < 0.2:
                    if dx == dy == 0:
                        ux, uy = 1.0, 0.0
                    else:
                        m = math.hypot(dx, dy); ux, uy = dx/m, dy/m
                else:
                    ux, uy = aimx/mag, aimy/mag
                self.ball.vx += ux * KICK_IMPULSE
                self.ball.vy += uy * KICK_IMPULSE
                self.last_event = f"{p.name} kicked!"
                self.last_event_t = time.time()
            p.cooldown = KICK_COOLDOWN

    def round_over(self):
        if self.score_red >= WIN_GOALS or self.score_blue >= WIN_GOALS:
            return True
        return time.time() >= self.round_end_ts

    def winner_text(self):
        if self.score_red > self.score_blue: return "Red wins!"
        if self.score_blue > self.score_red: return "Blue wins!"
        return "Draw!"

    def snapshot(self) -> dict:
        return {
            "t": time.time(),
            "field": [FIELD_W, FIELD_H, GOAL_W, GOAL_H],
            "score": [self.score_red, self.score_blue],
            "timer": max(0, int(self.round_end_ts - time.time())),
            "players": [
                {
                    "id": p.pid, "name": p.name, "team": p.team,
                    "x": round(p.x,2), "y": round(p.y,2),
                    "vx": round(p.vx,2), "vy": round(p.vy,2),
                    "stamina": round(p.stamina,1), "cooldown": round(p.cooldown,2),
                } for p in self.players.values()
            ],
            "ball": {"x": round(self.ball.x,2), "y": round(self.ball.y,2),
                     "vx": round(self.ball.vx,2), "vy": round(self.ball.vy,2)},
            "event": (self.last_event if (time.time()-self.last_event_t)<1.2 else "")
        }

game = Game()

async def handle_client(ws, path):
    # simple join handshake: receive {"type":"join","name":"..."}
    try:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "join" or len(game.players) >= MAX_PLAYERS:
            await ws.send(json.dumps({"type":"reject", "reason":"Server full or bad join"}))
            return

        pid = os.urandom(4).hex()
        name = msg.get("name","Player")
        game.add_player(pid, name)
        game.sockets[pid] = ws

        await ws.send(json.dumps({"type":"welcome","id":pid,"team":game.players[pid].team}))
        await game.broadcast({"type":"toast","msg":f"{name} joined!"})

        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("type") == "input":
                p = game.players.get(pid)
                if not p: continue
                p.last_input_ts = time.time()
                game.handle_input(p, data.get("data", {}), DT)
            elif data.get("type") == "ping":
                await ws.send(json.dumps({"type":"pong","t":data.get("t")}))
    except websockets.ConnectionClosed:
        pass
    finally:
        # disconnect
        # mark disconnected, remove after broadcast
        p = game.players.get(pid) if 'pid' in locals() else None
        if p:
            name = p.name
            game.remove_player(p.pid)
            await game.broadcast({"type":"toast","msg":f"{name} left."})

async def game_loop():
    last = time.time()
    while True:
        now = time.time()
        # fixed-ish tick
        await asyncio.sleep(max(0, DT - (now - last)))
        last = time.time()

        dt = DT
        # tick players
        for p in list(game.players.values()):
            if p.cooldown > 0: p.cooldown -= dt
            game.physics_player(p, dt)

        # tick ball
        game.physics_ball(dt)

        # end round?
        if game.round_over():
            await game.broadcast({"type":"toast","msg":game.winner_text()})
            # reset round
            game.score_red = game.score_blue = 0
            game.round_end_ts = time.time() + ROUND_SECONDS
            game.reset_positions(after_goal=False)

        # broadcast snapshot
        snap = game.snapshot()
        await game.broadcast({"type":"state","data":snap})

async def main():
    server = await websockets.serve(handle_client, "0.0.0.0", 8765, ping_interval=15, ping_timeout=15)
    print("Server listening on ws://0.0.0.0:8765")
    await game_loop()
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
