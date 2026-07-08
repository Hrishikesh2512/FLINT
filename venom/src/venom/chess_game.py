"""Voice chess — Venom as an opponent you play by speaking moves.

`python-chess` owns the board: legality, SAN/UCI parsing, and end-of-game
detection (checkmate / stalemate / draws). On top of it sits a compact
negamax + alpha-beta search with piece-square tables — strong enough for a
fun casual game, light enough to answer in ~a second on the Pi.

The voice brain converts speech to notation ("knight to f3" -> "Nf3") and
hands it to `human_move`; everything Venom says back is a natural phrase,
never raw notation, so it reads well through the headset.
"""

from __future__ import annotations

import logging
import random
import threading

import chess

log = logging.getLogger("venom.chess")

# Centipawn material values; king is effectively infinite (never captured).
VALUE = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
         chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
MATE = 1_000_000

# Piece-square tables, written a8-first (index 0 = a8 … 63 = h1) from White's
# point of view — the readable, chessprogramming-wiki layout. White reads
# `table[square_mirror(sq)]`, Black reads `table[sq]` (a vertical flip).
_PAWN = [
     0,  0,  0,  0,  0,  0,  0,  0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
     5,  5, 10, 25, 25, 10,  5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5, -5,-10,  0,  0,-10, -5,  5,
     5, 10, 10,-20,-20, 10, 10,  5,
     0,  0,  0,  0,  0,  0,  0,  0,
]
_KNIGHT = [
   -50,-40,-30,-30,-30,-30,-40,-50,
   -40,-20,  0,  0,  0,  0,-20,-40,
   -30,  0, 10, 15, 15, 10,  0,-30,
   -30,  5, 15, 20, 20, 15,  5,-30,
   -30,  0, 15, 20, 20, 15,  0,-30,
   -30,  5, 10, 15, 15, 10,  5,-30,
   -40,-20,  0,  5,  5,  0,-20,-40,
   -50,-40,-30,-30,-30,-30,-40,-50,
]
_BISHOP = [
   -20,-10,-10,-10,-10,-10,-10,-20,
   -10,  0,  0,  0,  0,  0,  0,-10,
   -10,  0,  5, 10, 10,  5,  0,-10,
   -10,  5,  5, 10, 10,  5,  5,-10,
   -10,  0, 10, 10, 10, 10,  0,-10,
   -10, 10, 10, 10, 10, 10, 10,-10,
   -10,  5,  0,  0,  0,  0,  5,-10,
   -20,-10,-10,-10,-10,-10,-10,-20,
]
_ROOK = [
     0,  0,  0,  0,  0,  0,  0,  0,
     5, 10, 10, 10, 10, 10, 10,  5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
     0,  0,  0,  5,  5,  0,  0,  0,
]
_QUEEN = [
   -20,-10,-10, -5, -5,-10,-10,-20,
   -10,  0,  0,  0,  0,  0,  0,-10,
   -10,  0,  5,  5,  5,  5,  0,-10,
    -5,  0,  5,  5,  5,  5,  0, -5,
     0,  0,  5,  5,  5,  5,  0, -5,
   -10,  5,  5,  5,  5,  5,  0,-10,
   -10,  0,  5,  0,  0,  0,  0,-10,
   -20,-10,-10, -5, -5,-10,-10,-20,
]
_KING = [  # midgame: hide behind pawns, off the centre
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -30,-40,-40,-50,-50,-40,-40,-30,
   -20,-30,-30,-40,-40,-30,-30,-20,
   -10,-20,-20,-20,-20,-20,-20,-10,
    20, 20,  0,  0,  0,  0, 20, 20,
    20, 30, 10,  0,  0, 10, 30, 20,
]
_PSQT = {chess.PAWN: _PAWN, chess.KNIGHT: _KNIGHT, chess.BISHOP: _BISHOP,
         chess.ROOK: _ROOK, chess.QUEEN: _QUEEN, chess.KING: _KING}

NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
         chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}

# Voice-friendly synonyms the brain (or the user, via the console) might send.
_SPOKEN = {
    "castle kingside": "O-O", "kingside castle": "O-O", "short castle": "O-O",
    "castle queenside": "O-O-O", "queenside castle": "O-O-O",
    "long castle": "O-O-O", "castles kingside": "O-O", "castles queenside": "O-O-O",
}


def _psqt(piece_type: int, square: int, white: bool) -> int:
    table = _PSQT[piece_type]
    return table[chess.square_mirror(square)] if white else table[square]


class ChessGame:
    """One game at a time. Thread-safe: voice and the web console share it."""

    def __init__(self, depth: int = 2):
        self._board: chess.Board | None = None
        self._human = chess.WHITE
        self._depth = depth
        self._lock = threading.Lock()

    # ── state ────────────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        with self._lock:
            return self._board is not None and not self._board.is_game_over()

    def board_text(self) -> str:
        """Unicode board for the console/logs (voice gets spoken text instead)."""
        with self._lock:
            if self._board is None:
                return "No game in progress."
            return self._board.unicode(borders=True, empty_square=".")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def new_game(self, color: str = "white", difficulty: int | None = None) -> str:
        human = chess.BLACK if str(color).strip().lower().startswith("b") else chess.WHITE
        with self._lock:
            self._board = chess.Board()
            self._human = human
            if difficulty is not None:
                self._depth = max(1, min(3, int(difficulty)))
            side = "White" if human == chess.WHITE else "Black"
            opening = ""
            if human == chess.BLACK:  # engine (White) opens
                move = self._pick_move()
                opening = " I'll open with " + self._describe(self._board, move) + "."
                self._board.push(move)
        return (f"New game — you're {side}, I'm the other side.{opening} "
                + ("Your move." if human == chess.WHITE else ""))

    def resign(self) -> str:
        with self._lock:
            if self._board is None:
                return "There's no game going."
            self._board = None
        return "Good game — I'll take the win. Rematch whenever you like."

    # ── play ─────────────────────────────────────────────────────────────────
    def human_move(self, move_text: str) -> str:
        with self._lock:
            if self._board is None:
                return "We're not playing yet — say 'start a chess game' first."
            if self._board.turn != self._human:
                return "It's my move, not yours."
            try:
                move = self._parse(self._board, move_text)
            except ValueError as exc:
                return str(exc)

            said = self._describe(self._board, move)
            self._board.push(move)
            over = self._verdict(self._board)
            if over:
                return f"You played {said}. {over}"

            reply = self._pick_move()
            my = self._describe(self._board, reply)
            self._board.push(reply)
            tail = self._verdict(self._board)
            if tail:
                return f"You played {said}. I'll play {my}. {tail}"
            check = " Check." if self._board.is_check() else ""
            return f"You played {said}. I'll play {my}.{check}"

    # ── move parsing (SAN preferred, UCI + spoken forms accepted) ─────────────
    def _parse(self, board: chess.Board, text: str) -> chess.Move:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("What move? Say something like 'knight to f3'.")
        cleaned = _SPOKEN.get(raw.lower(), raw)
        # SAN, as the brain should send it ("Nf3", "exd5", "e8=Q", "O-O").
        try:
            return board.parse_san(cleaned)
        except ValueError:
            pass
        # UCI fallback ("e2e4", "g1f3", "e7e8q").
        compact = cleaned.replace(" ", "").replace("-", "").lower()
        try:
            move = chess.Move.from_uci(compact)
            if move in board.legal_moves:
                return move
        except ValueError:
            pass
        # Case-fixed SAN: a leading piece letter wants upper ("nf3"->"Nf3"), a
        # leading file wants lower ("E4"->"e4") — voice/transcripts mangle case.
        head = cleaned[:1]
        if head in "nbrqk":
            fixed = head.upper() + cleaned[1:]
        elif head in "ABCDEFGH":
            fixed = head.lower() + cleaned[1:]
        else:
            fixed = ""
        if fixed:
            try:
                return board.parse_san(fixed)
            except ValueError:
                pass
        raise ValueError(f"'{raw}' isn't a legal move there — try again.")

    # ── engine ───────────────────────────────────────────────────────────────
    def _pick_move(self) -> chess.Move:
        board = self._board
        best, best_score = None, -MATE - 1
        moves = list(board.legal_moves)
        random.shuffle(moves)  # tie-break variety so games aren't identical
        moves.sort(key=lambda m: board.is_capture(m), reverse=True)  # order: captures first
        alpha = -MATE - 1
        for move in moves:
            board.push(move)
            score = -self._negamax(board, self._depth - 1, -MATE - 1, -alpha)
            board.pop()
            if score > best_score:
                best_score, best = score, move
                alpha = max(alpha, score)
        return best or moves[0]

    def _negamax(self, board: chess.Board, depth: int, alpha: int, beta: int) -> int:
        if board.is_game_over():
            if board.is_checkmate():
                return -MATE - depth  # prefer faster mates
            return 0  # stalemate / draw
        if depth <= 0:
            return self._evaluate(board)
        best = -MATE - 1
        moves = sorted(board.legal_moves, key=lambda m: board.is_capture(m),
                       reverse=True)
        for move in moves:
            board.push(move)
            score = -self._negamax(board, depth - 1, -beta, -alpha)
            board.pop()
            if score > best:
                best = score
            alpha = max(alpha, score)
            if alpha >= beta:
                break
        return best

    @staticmethod
    def _evaluate(board: chess.Board) -> int:
        score = 0
        for square, piece in board.piece_map().items():
            white = piece.color == chess.WHITE
            val = VALUE[piece.piece_type] + _psqt(piece.piece_type, square, white)
            score += val if white else -val
        return score if board.turn == chess.WHITE else -score

    # ── narration ────────────────────────────────────────────────────────────
    @staticmethod
    def _describe(board: chess.Board, move: chess.Move) -> str:
        if board.is_castling(move):
            return "castle kingside" if chess.square_file(move.to_square) > 4 \
                else "castle queenside"
        piece = board.piece_at(move.from_square)
        name = NAMES.get(piece.piece_type, "piece") if piece else "piece"
        verb = "takes on" if board.is_capture(move) else "to"
        phrase = f"{name} {verb} {chess.square_name(move.to_square)}"
        if move.promotion:
            phrase += f", promoting to {NAMES[move.promotion]}"
        return phrase

    def _verdict(self, board: chess.Board) -> str:
        """A closing line when the game just ended, else ''."""
        if not board.is_game_over():
            return ""
        result = ""
        if board.is_checkmate():
            # The side to move is mated → the mover before them won.
            winner_human = (board.turn != self._human)
            result = ("Checkmate — you got me. Well played!"
                      if winner_human else "Checkmate — I win. Good game!")
        elif board.is_stalemate():
            result = "Stalemate — it's a draw."
        elif board.is_insufficient_material():
            result = "Draw — neither of us has enough to mate."
        else:
            result = "That's a draw."
        self._board = None
        return result
