# client2.py — WebSocket version
import json, sys, threading, queue, asyncio
from math import trunc
from typing import List

import pygame
import websockets

pygame.init()

SERVER_IP = "192.168.1.83"
PORT = 5000
URI = sys.argv[1] if len(sys.argv) > 1 else "wss://quaint-rosalyn-1watershines1-26fb7e90.koyeb.app"

SIZE = (800, 1000)
SCREEN = pygame.display.set_mode(SIZE)
CLOCK  = pygame.time.Clock()
running = True
inbox: "queue.Queue[dict]" = queue.Queue()
outbox: "queue.Queue[dict]" = queue.Queue()
stop = threading.Event()
board = None
left_click = False
can_play = True
font = pygame.font.SysFont("Times New Roman", 150)

async def ws_loop(uri: str, inbox: queue.Queue, outbox: queue.Queue, stop_flag: threading.Event):
    try:
        async with websockets.connect(uri, max_size=2**20) as ws:
            # initial hello
            await ws.send(json.dumps({"type": "join", "name": "MacPlayer"}))
            await ws.send(json.dumps({"type": "request", "requested": "board"}))

            async def sender():
                # periodically check outbox and send
                while not stop_flag.is_set():
                    try:
                        obj = outbox.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    await ws.send(json.dumps(obj))

            async def receiver():
                while not stop_flag.is_set():
                    try:
                        text = await ws.recv()
                    except websockets.ConnectionClosed:
                        inbox.put({"type": "_closed"})
                        break
                    try:
                        inbox.put(json.loads(text))
                    except json.JSONDecodeError:
                        inbox.put({"type": "_raw", "text": text})

            await asyncio.gather(sender(), receiver())
    except OSError:
        inbox.put({"type": "_closed"})
    finally:
        inbox.put({"type": "_reader_done"})

def handle_events():
    global running, left_click
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            left_click = True
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            left_click = False

def draw_board(board: List[List[int]], surface, pos, left_click):
    global can_play
    cell_width, cell_height = trunc(SIZE[0] / 3), trunc(SIZE[1] / 30 * 8)
    if board is None:
        return
    hovered_row = pos[1] // cell_height
    hovered_col = pos[0] // cell_width
    for row in range(len(board)):
        for col in range(len(board[0])):
            colour = (200, 200, 200) if (hovered_row, hovered_col) == (row, col) and can_play else (255, 255, 255)
            pygame.draw.rect(surface, (0, 0, 0), (trunc(col * cell_width), trunc(row * cell_height), cell_width, cell_height), width=2)
            pygame.draw.rect(surface, colour, (trunc(col * cell_width) + 2, trunc(row * cell_height) + 2, cell_width - 4, cell_height - 4))
            if board[row][col] != "":
                label = font.render(str(board[row][col]), True, (0, 0, 0))
                text_rect = label.get_rect(center=pygame.Rect(trunc(col * cell_width) + 2, trunc(row * cell_height) + 2, cell_width - 4, cell_height - 4).center)
                surface.blit(label, text_rect)

    if left_click and can_play and 0 <= hovered_row < 3 and 0 <= hovered_col < 3:
        # queue the move for the websocket sender task
        outbox.put({"type": "move", "row": int(hovered_row), "col": int(hovered_col)})

def handle_inbox():
    global running, board, can_play
    try:
        while True:
            msg = inbox.get_nowait()
            t = msg.get("type")
            # print(msg)  # debug if needed
            if t in ("_closed", "_reader_done"):
                running = False
                break
            elif t == "board_update":
                board = msg["board"]
            elif t == "game_over":
                board = msg["board"]
                can_play = False
            elif t == "error":
                # optionally display an error on screen/log
                print("Server error:", msg.get("msg"))
            # else: info/message/etc
    except queue.Empty:
        pass

# Start websocket thread (runs its own asyncio loop)
ws_thread = threading.Thread(
    target=lambda: asyncio.run(ws_loop(URI, inbox, outbox, stop)),
    daemon=True
)
ws_thread.start()

# Main pygame loop
try:
    while running:
        SCREEN.fill((255, 255, 255))
        mouse_pos = pygame.mouse.get_pos()

        handle_events()
        draw_board(board, SCREEN, mouse_pos, left_click)
        handle_inbox()

        pygame.display.flip()
        CLOCK.tick(60)
finally:
    stop.set()
    ws_thread.join(timeout=1.0)
    pygame.quit()
