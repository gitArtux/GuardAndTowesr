import copy
from typing import Optional, Dict, Tuple

from guard_towers import BOARD_SIZE, DIRS, Board, coord_to_xy, parse_move

TTEntry = Dict[str, any]
TranspositionTable = Dict[int, TTEntry]
PVTable = Dict[int, str]

transposition_table: TranspositionTable = {}
pv_table: PVTable = {}

def copy_board(board: Board) -> Board:
    return copy.deepcopy(board)


def evaluate(board: Board, player: str) -> int:
    """
    Static evaluation combining material and simple positional factors.

    Material
    --------
    Sum of tower heights (each height counts as 100).

    Positional
    ----------
    * Guard progress toward its goal (+80 for every rank advanced).
    * Penalty for each enemy piece orthogonally adjacent to the guard (-200).

    These crude heuristics eliminate the biggest blind spots that made the
    search shuffle the guard aimlessly or walk into obvious tactical shots.
    """
    opp = 'r' if player == 'b' else 'b'

    # Immediate win / loss
    if board.guard[opp] == 0:
        return 10_000
    if board.guard[player] == 0:
        return -10_000

    score = 0

    # -------- material --------------------------------------------------#
    for h in range(7):
        score += (board.tower[player][h].bit_count() -
                  board.tower[opp][h].bit_count()) * (h + 1) * 100

    # -------- guard progress & safety ----------------------------------#
    my_guard_sq = board.find_guard(player)
    if my_guard_sq:
        gx, gy = coord_to_xy(my_guard_sq)
        progress = gy if player == 'b' else (6 - gy)
        score += progress * 80

        # penalty for danger
        for dx, dy in DIRS:
            nx, ny = gx + dx, gy + dy
            if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
                pc = board.grid[ny][nx]
                if pc and pc.color == opp:
                    score -= 200

    opp_guard_sq = board.find_guard(opp)
    if opp_guard_sq:
        ox, oy = coord_to_xy(opp_guard_sq)
        progress_opp = oy if opp == 'b' else (6 - oy)
        score -= progress_opp * 80

    return score



# Helper: adaptive search depth based on move count
def _adaptive_depth(board: Board, player: str, base_depth: int = 5) -> int:
    """
    Pick a search depth that grows when the position is simpler.

    Heuristic:
    - Many legal moves  ( > 25 )      → keep base depth.
    - Medium legal moves (16–25)      → +1 ply.
    - Few legal moves   ( 8–15)       → +2 plies.
    - Very few moves    ( ≤ 7)        → +3 plies.

    This is cheap (just counts moves) and tracks the real branching factor,
    which is what makes deeper search affordable once material comes off.
    """
    num_moves = len(board.generate_moves(player))
    if num_moves <= 7:
        return base_depth + 3
    if num_moves <= 15:
        return base_depth + 2
    if num_moves <= 25:
        return base_depth + 1
    return base_depth


def alphabeta(board: Board, depth: int, alpha: int, beta: int, maximizing: bool, player: str) -> int:
    """
    Perform the alpha-beta pruning minimax search to evaluate the best achievable score from the current board state.

    Args:
        board (Board): The current game board state.
        depth (int): The maximum search depth.
        alpha (int): The alpha value for pruning (best already explored option along the path to the maximizer).
        beta (int): The beta value for pruning (best already explored option along the path to the minimizer).
        maximizing (bool): True if the current move is for the maximizing player, False otherwise.
        player (str): The player color ('b' or 'r') for whom the evaluation is done.

    Returns:
        int: The evaluated score of the board state from the perspective of the player.
    """
    # Preserve the original alpha–beta window so we can label the TT entry correctly
    alpha_orig = alpha
    beta_orig  = beta

    def move_sort_key(m):
        if m == pv_move:
            return float('inf')
        frm, to, n = parse_move(m)
        dest = board.piece_at(to)
        if dest:
            if dest.kind == 'guard':
                return 1000
            return dest.height
        return 0

    opponent = 'r' if player == 'b' else 'b'
    zobrist = board.zobrist_hash()
    entry = transposition_table.get(zobrist)

    # Use TT if valid
    if entry and entry['depth'] >= depth:
        if entry['type'] == 'exact':
            return entry['score']
        elif entry['type'] == 'alpha' and entry['score'] <= alpha:
            return alpha
        elif entry['type'] == 'beta' and entry['score'] >= beta:
            return beta

    # Terminal: guard captured?
    if board.find_guard(opponent) is None or board.find_guard(player) is None:
        return evaluate(board, player)
    # Depth zero → quiescence
    if depth == 0:
        return quiescence(board, alpha, beta, player)

    # Null‑move pruning (depth reduction R = 2)
    if depth > 2:
        board.apply_null_move()
        score_nm = -alphabeta(board, depth - 1 - 2, -beta, -beta + 1, not maximizing, player)
        board.unapply_null_move()
        if score_nm >= beta:
            return beta

    # Move ordering: PV-first then MVV-LVA
    zob = board.zobrist_hash()
    tt_ent = transposition_table.get(zob, {})
    pv_move = tt_ent.get('best_move')
    best_move = None
    if maximizing:
        max_eval = -float('inf')
        moves = board.generate_moves(player)
        moves.sort(key=move_sort_key, reverse=True)
        for move in moves:
            try:
                frm, to, n = parse_move(move)
                board.apply_move(player, frm, to, n)
            except Exception:
                continue
            try:
                eval = alphabeta(board, depth - 1, alpha, beta, False, player)
            finally:
                board.unapply_move()
            if eval > max_eval:
                max_eval = eval
                best_move = move
            alpha = max(alpha, eval)
            if beta <= alpha:
                break
        score = max_eval
    else:
        min_eval = float('inf')
        moves = board.generate_moves(opponent)
        moves.sort(key=move_sort_key, reverse=True)
        for move in moves:
            try:
                frm, to, n = parse_move(move)
                board.apply_move(opponent, frm, to, n)
            except Exception:
                continue
            try:
                eval = alphabeta(board, depth - 1, alpha, beta, True, player)
            finally:
                board.unapply_move()
            if eval < min_eval:
                min_eval = eval
                best_move = move
            beta = min(beta, eval)
            if beta <= alpha:
                break
        score = min_eval

    # Store in TT with replacement policy
    node_type = 'exact'
    if score <= alpha_orig:
        node_type = 'alpha'
    elif score >= beta_orig:
        node_type = 'beta'
    old = transposition_table.get(zobrist)
    if not old or depth >= old['depth']:
        transposition_table[zobrist] = {
            'score': score,
            'depth': depth,
            'type': node_type,
            'best_move': best_move
        }

    # Save PV
    if best_move:
        pv_table[zobrist] = best_move

    return score


def find_best_move(board: Board, player: str, base_depth: int = 5) -> Optional[str]:
    """
    Find the best move for the given player by searching the game tree up to the specified depth using alpha-beta pruning.

    The function evaluates all possible moves for the player and selects the move that leads to the highest evaluated score.

    Args:
        board (Board): The current game board state.
        player (str): The player color ('b' or 'r') to find the best move for.
        base_depth (int, optional): The search depth limit. Defaults to 5.

    Returns:
        str: The best move in string format, or None if no moves are available.
    """
    depth = _adaptive_depth(board, player, base_depth)
    best_move = None
    best_score = -float('inf')
    root_zob = board.zobrist_hash()
    for d in range(1, depth + 1):
        alpha = -float('inf') if d == 1 else best_score - 50
        beta  =  float('inf') if d == 1 else best_score + 50
        best_score = alphabeta(board, d, alpha, beta, True, player)
        best_move = pv_table.get(root_zob, best_move)
    return best_move


def quiescence(board: Board, alpha: int, beta: int, player: str) -> int:
    """
    Quiescence search: only explore capture moves to avoid horizon effects.
    """
    stand_pat = evaluate(board, player)
    if stand_pat >= beta:
        return beta
    if alpha < stand_pat:
        alpha = stand_pat
    opponent = 'r' if player == 'b' else 'b'
    for move in board.generate_moves(player):
        frm, to, n = parse_move(move)
        dest = board.piece_at(to)
        if dest is None or dest.color == player:
            continue
        try:
            board.apply_move(player, frm, to, n)
        except:
            continue
        score = -quiescence(board, -beta, -alpha, opponent)
        board.unapply_move()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha