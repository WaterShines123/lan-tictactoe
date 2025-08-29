# server.py — WebSocket TicTacToe with restart + QoL
import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import websockets
from websockets.server import WebSocketServerProtocol as WSS

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "5000"))

# ====== Game Config ======
MARKS = ["X", "O"]
BOARD_SIZE = 3
ENABLE_TURN_TIMER = False        # set True to enforce per-turn timer
TURN_SECONDS = 20                # turn time if enabled
PING_INTERVAL = 25               # seconds between heartbeats

# ====== Utility ======
async def send_json(ws: WSS, obj: Any):
    await ws.send(json.dumps(obj))

async def broadcast(targets: Set[WSS], obj: Any):
    if not targets:
        return
    await asyncio.gather(*(send_json(ws, obj) for ws in list(targets)), return_exceptions=True)

def now_ms() -> int:
    return int(time.time() * 1000)

# ====== Game Logic ======
class TicTacToe:
    def __init__(self):
        self.board: List[List[str]] = [[""] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self.turn: str = "O"  # will be overwritten by Room.start_new_game
        self.winner: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"board": self.board, "turn": self.turn, "winner": self.winner}

    def _line_winner(self, line: List[str]) -> str:
        return line[0] if line[0] != "" and all(c == line[0] for c in line) else ""

    def check_win(self) -> str:
        # rows
        for r in self.board:
            w = self._line_winner(r)
            if w:
                return w
        # cols
        for c in range(BOARD_SIZE):
            col = [self.board[r][c] for r in range(BOARD_SIZE)]
            w = self._line_winner(col)
            if w:
                return w
        # diags
        main = [self.board[i][i] for i in range(BOARD_SIZE)]
        w = self._line_winner(main)
        if w:
            return w
        anti = [self.board[i][BOARD_SIZE - 1 - i] for i in range(BOARD_SIZE)]
        w = self._line_winner(anti)
        if w:
            return w
        return ""

    def is_full(self) -> bool:
        return all(self.board[r][c] != "" for r in range(BOARD_SIZE) for c in range(BOARD_SIZE))

    def play(self, row: int, col: int, mark: str) -> Dict[str, Any]:
        if self.winner:
            return {"type": "error", "code": "game_finished", "msg": "Game already finished."}
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return {"type": "error", "code": "bad_coords", "msg": "Invalid coordinates."}
        if self.board[row][col] != "":
            return {"type": "error", "code": "space_full", "msg": "That square is taken."}
        if self.turn != mark:
            return {"type": "error", "code": "not_your_turn", "msg": "It is not your turn."}

        self.board[row][col] = mark
        win = self.check_win()
        if win:
            self.winner = win
            return {"type": "game_over", "board": self.board, "winner": win}
        if self.is_full():
            self.winner = "draw"
            return {"type": "game_over", "board": self.board, "winner": "draw"}

        self.turn = "O" if self.turn == "X" else "X"
        return {"type": "board_update", "board": self.board, "turn": self.turn}

# ====== Room / Session ======
class Room:
    def __init__(self):
        self.players: Dict[str, Optional[WSS]] = {"X": None, "O": None}
        self.player_names: Dict[str, str] = {"X": "X", "O": "O"}
        self.spectators: Set[WSS] = set()
        self.role_by_ws: Dict[WSS, str] = {}  # "X"|"O"|"spectator"
        self.scores: Dict[str, int] = {"X": 0, "O": 0}
        self.game = TicTacToe()
        self.awaiting_restart_from: Set[str] = set()  # marks that requested restart
        self.next_starting_mark = "O"  # alternates each new game
        self.turn_deadline_ms: Optional[int] = None

    # ---- assignment ----
    def assign_role(self, ws: WSS) -> str:
        for m in MARKS:
            if self.players[m] is None:
                self.players[m] = ws
                self.role_by_ws[ws] = m
                return m
        # otherwise spectator
        self.spectators.add(ws)
        self.role_by_ws[ws] = "spectator"
        return "spectator"

    def name_for(self, mark: str) -> str:
        return self.player_names.get(mark, mark)

    def opponent_mark(self, mark: str) -> Optional[str]:
        if mark not in MARKS:
            return None
        return "O" if mark == "X" else "X"

    def ws_for_mark(self, mark: str) -> Optional[WSS]:
        return self.players.get(mark)

    def drop_ws(self, ws: WSS):
        role = self.role_by_ws.pop(ws, None)
        if role in MARKS:
            self.players[role] = None
            # if a player leaves mid game, no auto-reset; remaining can wait for someone to join
        elif role == "spectator":
            self.spectators.discard(ws)

    # ---- game control ----
    def start_new_game(self):
        self.game = TicTacToe()
        # alternate starting player
        self.game.turn = self.next_starting_mark
        self.next_starting_mark = "O" if self.game.turn == "X" else "X"
        self.awaiting_restart_from.clear()
        self._maybe_reset_turn_deadline()

    def _maybe_reset_turn_deadline(self):
        if ENABLE_TURN_TIMER:
            self.turn_deadline_ms = now_ms() + TURN_SECONDS * 1000
        else:
            self.turn_deadline_ms = None

    def record_game_over(self, winner: str):
        if winner in MARKS:
            self.scores[winner] += 1

    def everyone(self) -> Set[WSS]:
        s: Set[WSS] = set(self.spectators)
        for m in MARKS:
            if self.players[m] is not None:
                s.add(self.players[m])
        return s

    def state_payload(self) -> Dict[str, Any]:
        return {
            "type": "state",
            "game": self.game.as_dict(),
            "scores": self.scores,
            "players": {m: (self.player_names[m] if self.players[m] else None) for m in MARKS},
            "turn_deadline_ms": self.turn_deadline_ms,
        }

    async def push_state(self):
        await broadcast(self.everyone(), self.state_payload())

room = Room()

# ====== Timer Task (optional) ======
async def turn_timer_task():
    while True:
        await asyncio.sleep(0.5)
        if not ENABLE_TURN_TIMER:
            continue
        if room.game.winner or room.game.turn not in MARKS:
            continue
        if room.turn_deadline_ms is None:
            continue
        if now_ms() >= room.turn_deadline_ms:
            # time up: opponent wins
            winner = room.opponent_mark(room.game.turn)
            room.game.winner = winner
            if winner in MARKS:
                room.record_game_over(winner)  # score
            await broadcast(room.everyone(), {
                "type": "game_over",
                "board": room.game.board,
                "winner": winner or "draw",
                "reason": "timeout",
            })
            # After game over, keep state visible
            await room.push_state()

# ====== Heartbeat ======
async def ping_task():
    while True:
        await asyncio.sleep(PING_INTERVAL)
        targets = list(room.everyone())
        if not targets:
            continue
        await asyncio.gather(*(ws.ping() for ws in targets), return_exceptions=True)

# ====== Handler ======
async def handler(ws: WSS):
    # Assign role
    role = room.assign_role(ws)
    try:
        # Initial handshake: expect optional join {type:"join", name:"..."}
        await send_json(ws, {"type": "hello", "role": role})
        # Send full room state
        await room.push_state()

        if role == "spectator":
            await send_json(ws, {"type": "message", "msg": "You are watching as a spectator."})
        elif role in MARKS:
            # Inform who you are and whether waiting for opponent
            if room.opponent_mark(role) and room.ws_for_mark(room.opponent_mark(role)) is None:
                await send_json(ws, {"type": "message", "msg": "Waiting for the other player..."})

        async for text in ws:
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                await send_json(ws, {"type": "error", "code": "bad_json", "msg": "Invalid JSON."})
                continue

            mtype = msg.get("type")
            # --- join / rename ---
            if mtype == "join":
                # client may send join again with a name
                if role in MARKS:
                    name = str(msg.get("name") or role)[:32]
                    room.player_names[role] = name
                    await room.push_state()
                else:
                    await send_json(ws, {"type": "message", "msg": "Spectating. Names only apply to players."})
                continue

            # --- request board/state ---
            if mtype == "request" and msg.get("requested") in ("board", "state"):
                await room.push_state()
                continue

            # --- chat ---
            if mtype == "chat":
                txt = str(msg.get("msg", ""))[:200]
                if not txt:
                    continue
                author = role if role in MARKS else "spectator"
                await broadcast(room.everyone(), {"type": "chat", "from": author, "name": room.player_names.get(author, author), "msg": txt})
                continue

            # --- move ---
            if mtype == "move":
                if role not in MARKS:
                    await send_json(ws, {"type": "error", "code": "not_player", "msg": "Spectators cannot move."})
                    continue
                row = msg.get("row")
                col = msg.get("col")
                if not isinstance(row, int) or not isinstance(col, int):
                    await send_json(ws, {"type": "error", "code": "bad_coords", "msg": "row/col must be integers."})
                    continue
                result = room.game.play(row, col, role)
                if result["type"] == "board_update":
                    # reset timer
                    room._maybe_reset_turn_deadline()
                    await broadcast(room.everyone(), {"type": "board_update", "board": result["board"], "turn": result["turn"], "turn_deadline_ms": room.turn_deadline_ms})
                elif result["type"] == "game_over":
                    winner = result.get("winner")
                    if winner in MARKS:
                        room.record_game_over(winner)
                    await broadcast(room.everyone(), result)
                    await room.push_state()
                else:
                    await send_json(ws, result)
                continue

            # --- resign ---
            if mtype == "resign":
                if role not in MARKS:
                    await send_json(ws, {"type": "error", "code": "not_player", "msg": "Spectators cannot resign."})
                    continue
                winner = room.opponent_mark(role) or "draw"
                room.game.winner = winner
                if winner in MARKS:
                    room.record_game_over(winner)
                await broadcast(room.everyone(), {"type": "game_over", "board": room.game.board, "winner": winner, "reason": "resignation"})
                await room.push_state()
                continue

            # --- restart flow ---
            if mtype == "restart_request":
                if role not in MARKS:
                    await send_json(ws, {"type": "error", "code": "not_player", "msg": "Only players can request restart."})
                    continue
                room.awaiting_restart_from.add(role)
                # notify the other player
                other = room.opponent_mark(role)
                other_ws = room.ws_for_mark(other) if other else None
                if other_ws:
                    await send_json(other_ws, {"type": "restart_offer", "from": role, "name": room.name_for(role)})
                # If both agreed OR game already finished and single request is enough
                if (other in room.awaiting_restart_from) or (room.game.winner is not None):
                    room.start_new_game()
                    await broadcast(room.everyone(), {
                        "type": "new_game",
                        "board": room.game.board,
                        "starting_mark": room.game.turn,
                        "scores": room.scores,
                        "turn_deadline_ms": room.turn_deadline_ms,
                    })
                    await room.push_state()
                else:
                    await send_json(ws, {"type": "message", "msg": "Restart request sent. Waiting for the other player."})
                continue

            # --- accept restart (alias) ---
            if mtype == "restart_accept":
                # same as sending a restart_request from that player
                if role in MARKS:
                    room.awaiting_restart_from.add(role)
                    other = room.opponent_mark(role)
                    if (other in room.awaiting_restart_from) or (room.game.winner is not None):
                        room.start_new_game()
                        await broadcast(room.everyone(), {
                            "type": "new_game",
                            "board": room.game.board,
                            "starting_mark": room.game.turn,
                            "scores": room.scores,
                            "turn_deadline_ms": room.turn_deadline_ms,
                        })
                        await room.push_state()
                continue

            # --- unknown ---
            await send_json(ws, {"type": "error", "code": "unknown_type", "msg": f"Unknown message type: {mtype}"})
    finally:
        room.drop_ws(ws)
        # Let others know someone left + refresh state
        await room.push_state()

# ====== Server ======
async def main():
    print(f"WebSocket server listening on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT, max_size=2**20, ping_interval=None):
        # start background tasks
        await asyncio.gather(turn_timer_task(), ping_task())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
