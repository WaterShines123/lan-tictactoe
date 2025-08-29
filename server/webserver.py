# server.py — WebSocket version
import asyncio
import json
import os

import websockets
from typing import Dict, Optional, Any, List

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "5000"))  # Koyeb sets PORT
MARKS = ["X", "O"]  # keep X/O like before

class TicTacToe:
    def __init__(self):
        self.board: List[List[str]] = [["", "", ""], ["", "", ""], ["", "", ""]]
        self.turn = "O"

    def _check_rows(self, board: List[List[str]]) -> str:
        for row in board:
            if row[0] != "" and len(set(row)) == 1:
                return row[0]
        return ""

    def _check_diagonals(self) -> str:
        # main diag
        diag1 = [self.board[i][i] for i in range(3)]
        if diag1[0] != "" and len(set(diag1)) == 1:
            return diag1[0]
        # anti diag
        diag2 = [self.board[i][2 - i] for i in range(3)]
        if diag2[0] != "" and len(set(diag2)) == 1:
            return diag2[0]
        return ""

    def check_win(self) -> str:
        # rows
        r = self._check_rows(self.board)
        if r:
            return r
        # columns (transpose without numpy)
        cols = list(map(list, zip(*self.board)))
        c = self._check_rows(cols)
        if c:
            return c
        return self._check_diagonals()

    def play(self, row: int, col: int, attempted_mark: str) -> Dict[str, Any]:
        if not (0 <= row < 3 and 0 <= col < 3):
            return {"type": "error", "msg": "invalid move coordinates"}
        if self.board[row][col] != "":
            return {"type": "error", "msg": "this space is already full"}
        if self.turn != attempted_mark:
            return {"type": "error", "msg": "not your go"}
        self.board[row][col] = self.turn
        self.turn = "X" if self.turn == "O" else "O"
        winner = self.check_win()
        if winner:
            return {"type": "game_over", "board": self.board, "winner": winner}
        return {"type": "board_update", "board": self.board}

async def send_json(ws: websockets.WebSocketServerProtocol, obj: Any):
    await ws.send(json.dumps(obj))

class Room:
    def __init__(self):
        self.players: Dict[str, Optional[websockets.WebSocketServerProtocol]] = {"X": None, "O": None}
        self.by_ws: Dict[websockets.WebSocketServerProtocol, str] = {}
        self.game = TicTacToe()

    def has_spot(self) -> bool:
        return any(self.players[m] is None for m in MARKS)

    def assign_mark(self, ws: websockets.WebSocketServerProtocol) -> Optional[str]:
        for m in MARKS:
            if self.players[m] is None:
                self.players[m] = ws
                self.by_ws[ws] = m
                return m
        return None

    def remove(self, ws: websockets.WebSocketServerProtocol):
        m = self.by_ws.pop(ws, None)
        if m is not None:
            self.players[m] = None

    def opponent(self, mark: str) -> Optional[websockets.WebSocketServerProtocol]:
        other = "O" if mark == "X" else "X"
        return self.players[other]

    async def broadcast(self, obj: Any):
        tasks = []
        for ws in self.players.values():
            if ws is not None:
                tasks.append(send_json(ws, obj))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

room = Room()

async def handler(ws: websockets.WebSocketServerProtocol):
    if not room.has_spot():
        await send_json(ws, {"type": "error", "msg": "Room full (2 players max)."})
        await ws.close()
        return

    mark = room.assign_mark(ws)
    await send_json(ws, {"type": "info", "msg": f"Welcome to Tic Tac Toe, your mark is {mark}"})
    # Mirror original behavior: tell first player to wait
    if room.opponent(mark) is None:
        await send_json(ws, {"type": "message", "msg": "Please wait for the other player"})

    try:
        async for text in ws:
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                await send_json(ws, {"type": "error", "msg": "invalid JSON"})
                continue

            t = msg.get("type")

            if t == "request" and msg.get("requested") == "board":
                await send_json(ws, {"type": "board_update", "board": room.game.board})
                continue

            if t == "move":
                row = msg.get("row")
                col = msg.get("col")
                if not isinstance(row, int) or not isinstance(col, int):
                    await send_json(ws, {"type": "error", "msg": "row/col must be integers"})
                    continue
                # sender's mark
                sender_mark = room.by_ws.get(ws)
                if sender_mark is None:
                    await send_json(ws, {"type": "error", "msg": "you are not in the game"})
                    continue
                result = room.game.play(row, col, sender_mark)
                # broadcast board updates & game over to both
                if result["type"] in ("board_update", "game_over"):
                    await room.broadcast(result)
                else:
                    await send_json(ws, result)
                continue

            # Unknown messages are ignored (or you can send an error)
    finally:
        room.remove(ws)
        # Optionally notify remaining player
        # await room.broadcast({"type":"message","msg":"A player disconnected"})

async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=2**20):
        print(f"WebSocket server listening on ws://{HOST}:{PORT}")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
