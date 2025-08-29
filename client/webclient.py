# client2.py — Pygame WebSocket client with Restart/Resign buttons, chat, status bar
import asyncio
import json
import sys
import threading
import queue
from typing import List, Optional, Tuple

import pygame
import websockets

# ---------- Config ----------
DEFAULT_URI = "wss://quaint-rosalyn-1watershines1-26fb7e90.koyeb.app"
URI = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URI

WIDTH, HEIGHT = 800, 1000
BOARD_ROWS, BOARD_COLS = 3, 3
FPS = 60

# Layout
BOARD_H = int(HEIGHT * 0.66)
CELL_W = WIDTH // BOARD_COLS
CELL_H = BOARD_H // BOARD_ROWS
PANEL_Y = BOARD_H + 10
BTN_W, BTN_H = 160, 50
MARGIN = 16

# Colors
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREY = (220, 220, 220)
DARK = (40, 40, 40)
GREEN = (0, 170, 0)
RED = (200, 40, 40)
BLUE = (30, 100, 220)
YELLOW = (220, 160, 0)

pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("TicTacToe (WebSocket)")
clock = pygame.time.Clock()

# Fonts
font_big = pygame.font.SysFont("Arial", 120)
font = pygame.font.SysFont("Arial", 24)
font_small = pygame.font.SysFont("Arial", 18)

# Networking mailboxes
inbox: "queue.Queue[dict]" = queue.Queue()
outbox: "queue.Queue[dict]" = queue.Queue()
stop_flag = threading.Event()

# Client state
role: Optional[str] = None            # "X" | "O" | "spectator"
name_me: str = ""                     # my display name (if server echoes it in state)
names = {"X": "X", "O": "O"}
scores = {"X": 0, "O": 0}
board: List[List[str]] = [[""] * BOARD_COLS for _ in range(BOARD_ROWS)]
turn: Optional[str] = None
winner: Optional[str] = None
turn_deadline_ms: Optional[int] = None

connected = False
disconnect_reason = ""
restart_offer_from: Optional[str] = None  # "X" or "O" when the other asks to restart

# Chat
chat_lines: List[Tuple[str, str]] = []  # (name, msg)
chat_input_active = False
chat_text = ""

# Buttons (rects computed later)
restart_btn = pygame.Rect(MARGIN, PANEL_Y, BTN_W, BTN_H)
resign_btn = pygame.Rect(MARGIN + BTN_W + 10, PANEL_Y, BTN_W, BTN_H)
# Chat input box
chat_box = pygame.Rect(MARGIN, PANEL_Y + BTN_H + 12, WIDTH - MARGIN * 2, 34)

# ---------- WebSocket tasks ----------
async def ws_loop(uri: str, inbox: queue.Queue, outbox: queue.Queue, stop_flag: threading.Event):
    global connected
    try:
        async with websockets.connect(uri, max_size=2**20) as ws:
            connected = True
            # Say hello and ask for state
            await ws.send(json.dumps({"type": "join", "name": "Player"}))
            await ws.send(json.dumps({"type": "request", "requested": "state"}))

            async def sender():
                while not stop_flag.is_set():
                    try:
                        obj = outbox.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    try:
                        await ws.send(json.dumps(obj))
                    except Exception:
                        # Connection lost
                        break

            async def receiver():
                nonlocal ws
                while not stop_flag.is_set():
                    try:
                        text = await ws.recv()
                    except websockets.ConnectionClosed as e:
                        inbox.put({"type": "_closed", "code": e.code, "reason": str(e.reason)})
                        break
                    except Exception as e:
                        inbox.put({"type": "_closed", "reason": repr(e)})
                        break
                    try:
                        inbox.put(json.loads(text))
                    except json.JSONDecodeError:
                        # push raw
                        inbox.put({"type": "_raw", "text": text})

            await asyncio.gather(sender(), receiver())
    except Exception as e:
        inbox.put({"type": "_closed", "reason": repr(e)})
    finally:
        connected = False
        inbox.put({"type": "_reader_done"})

def start_ws_thread():
    t = threading.Thread(target=lambda: asyncio.run(ws_loop(URI, inbox, outbox, stop_flag)), daemon=True)
    t.start()
    return t

# ---------- Drawing helpers ----------
def draw_status_bar():
    # Names & scores & turn
    left = MARGIN
    right = WIDTH - MARGIN

    # role bubble
    role_str = f"You: {role or '-'}"
    role_color = BLUE if role in ("X", "O") else GREY
    pygame.draw.rect(screen, role_color, (left, 8, 120, 28), border_radius=8)
    txt = font_small.render(role_str, True, WHITE)
    screen.blit(txt, (left + 8, 12))

    # names & scores center
    center_text = f"{names.get('X','X')} (X) {scores.get('X',0)}  —  {scores.get('O',0)} (O) {names.get('O','O')}"
    ct = font.render(center_text, True, BLACK)
    screen.blit(ct, (WIDTH//2 - ct.get_width()//2, 10))

    # turn / winner right
    status = ""
    if winner:
        status = "Draw!" if winner == "draw" else f"{winner} wins!"
    elif turn:
        status = f"Turn: {turn}"
    rt = font.render(status, True, BLACK)
    screen.blit(rt, (right - rt.get_width(), 10))

    # timer
    if turn_deadline_ms:
        import time as _t
        ms_left = max(0, turn_deadline_ms - int(_t.time() * 1000))
        sec = (ms_left + 999)//1000
        tt = font_small.render(f"Timer: {sec}s", True, (120, 0, 0))
        screen.blit(tt, (right - tt.get_width(), 40))

def draw_board():
    # Grid
    for r in range(BOARD_ROWS):
        for c in range(BOARD_COLS):
            x, y = c * CELL_W, r * CELL_H
            rect = pygame.Rect(x, y, CELL_W, CELL_H)
            pygame.draw.rect(screen, BLACK, rect, width=2)
            val = board[r][c]
            if val:
                label = font_big.render(val, True, BLACK)
                lr = label.get_rect(center=rect.center)
                screen.blit(label, lr)

def draw_buttons():
    # Restart
    pygame.draw.rect(screen, GREEN, restart_btn, border_radius=8)
    rt = font.render("Restart", True, WHITE)
    screen.blit(rt, (restart_btn.centerx - rt.get_width()//2, restart_btn.centery - rt.get_height()//2))

    # Resign (disabled for spectators)
    color = RED if role in ("X","O") else GREY
    pygame.draw.rect(screen, color, resign_btn, border_radius=8)
    tt = font.render("Resign", True, WHITE)
    screen.blit(tt, (resign_btn.centerx - tt.get_width()//2, resign_btn.centery - tt.get_height()//2))

    # Restart offer banner, if any
    if restart_offer_from:
        msg = f"{restart_offer_from} asked to restart. Click Restart to accept."
        banner = font.render(msg, True, YELLOW)
        screen.blit(banner, (MARGIN, restart_btn.bottom + 8))

def draw_chat():
    # input box
    pygame.draw.rect(screen, WHITE, chat_box, border_radius=8)
    pygame.draw.rect(screen, BLACK, chat_box, width=2, border_radius=8)
    hint = "> " + (chat_text if chat_input_active else (chat_text or "Type message…"))
    col = BLACK if chat_input_active or chat_text else (130, 130, 130)
    t = font.render(hint[:120], True, col)
    screen.blit(t, (chat_box.x + 8, chat_box.y + 6))

    # chat log (last 8 lines)
    y = chat_box.bottom + 8
    max_lines = 8
    for name, msg in chat_lines[-max_lines:]:
        line = font_small.render(f"{name}: {msg}", True, BLACK)
        screen.blit(line, (MARGIN, y))
        y += line.get_height() + 2

def draw_connection_overlay():
    # dim
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 120))
    screen.blit(overlay, (0, 0))
    # text
    msg = "Connecting…" if not connected else "Disconnected"
    t = font.render(msg, True, WHITE)
    screen.blit(t, (WIDTH//2 - t.get_width()//2, HEIGHT//2 - t.get_height()//2))

def inside_board(mx, my) -> Optional[Tuple[int,int]]:
    if my >= BOARD_H: return None
    r = my // CELL_H
    c = mx // CELL_W
    if 0 <= r < BOARD_ROWS and 0 <= c < BOARD_COLS:
        return int(r), int(c)
    return None

# ---------- Inbox handler ----------
def handle_inbox():
    global role, names, scores, board, turn, winner, restart_offer_from, turn_deadline_ms, name_me, disconnect_reason
    try:
        while True:
            msg = inbox.get_nowait()
            t = msg.get("type")

            if t == "_closed":
                disconnect_reason = msg.get("reason", "")
                # keep running so overlay shows
                continue

            if t == "hello":
                role = msg.get("role")
                continue

            if t == "state":
                g = msg.get("game", {})
                board = g.get("board", board)
                turn = g.get("turn", turn)
                winner = g.get("winner", winner)
                scores.update(msg.get("scores", scores))
                players = msg.get("players", {})
                names["X"] = players.get("X") or "X"
                names["O"] = players.get("O") or "O"
                turn_deadline_ms = msg.get("turn_deadline_ms")
                name_me = names.get(role or "", "") if role in ("X","O") else "spectator"
                continue

            if t == "board_update":
                board = msg.get("board", board)
                turn = msg.get("turn", turn)
                turn_deadline_ms = msg.get("turn_deadline_ms", turn_deadline_ms)
                winner = None
                continue

            if t == "game_over":
                board = msg.get("board", board)
                winner = msg.get("winner")
                continue

            if t == "new_game":
                board = msg.get("board", board)
                winner = None
                turn = msg.get("starting_mark", "O")
                scores.update(msg.get("scores", scores))
                turn_deadline_ms = msg.get("turn_deadline_ms")
                restart_offer_from = None
                continue

            if t == "restart_offer":
                restart_offer_from = msg.get("from")
                continue

            if t == "chat":
                nm = msg.get("name") or msg.get("from") or "?"
                chat_lines.append((nm, msg.get("msg","")))
                continue

            if t == "message":
                chat_lines.append(("server", msg.get("msg","")))
                continue

            if t == "error":
                chat_lines.append(("error", f"{msg.get('code','')}: {msg.get('msg','')}"))
                continue

            # ignore unknowns
    except queue.Empty:
        pass

# ---------- Event handling ----------
def handle_mouse(mx, my, pressed):
    global restart_offer_from
    if pressed[0]:
        # Buttons
        if restart_btn.collidepoint(mx, my):
            outbox.put({"type": "restart_request"})
            return
        if resign_btn.collidepoint(mx, my) and role in ("X","O"):
            outbox.put({"type": "resign"})
            return
        # Board click -> move
        idx = inside_board(mx, my)
        if idx and role in ("X","O"):
            r, c = idx
            outbox.put({"type": "move", "row": r, "col": c})

def handle_key(event):
    global chat_input_active, chat_text
    if event.key == pygame.K_RETURN:
        if chat_text.strip():
            outbox.put({"type": "chat", "msg": chat_text.strip()[:180]})
            chat_text = ""
        chat_input_active = not chat_input_active  # toggle focus on Enter
    elif chat_input_active:
        if event.key == pygame.K_BACKSPACE:
            chat_text = chat_text[:-1]
        else:
            # only allow reasonable characters
            ch = event.unicode
            if ch and ch.isprintable():
                chat_text += ch

# ---------- Main ----------
def main():
    ws_thread = start_ws_thread()

    try:
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    handle_key(event)

            mx, my = pygame.mouse.get_pos()
            pressed = pygame.mouse.get_pressed(num_buttons=3)

            # Networking
            handle_inbox()

            # Drawing
            screen.fill(WHITE)
            draw_status_bar()
            draw_board()
            draw_buttons()
            draw_chat()

            # Clicks (after drawing so button rects are valid)
            if any(pressed):
                handle_mouse(mx, my, pressed)

            # Connection overlay
            if not connected:
                draw_connection_overlay()

            pygame.display.flip()
            clock.tick(FPS)
    finally:
        stop_flag.set()
        ws_thread.join(timeout=1.0)
        pygame.quit()

if __name__ == "__main__":
    main()
