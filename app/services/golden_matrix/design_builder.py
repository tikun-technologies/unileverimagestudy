"""
Two-layer design builder for the Golden Matrix.

RespondentBuilder  — constructs a single T×P block for one respondent,
                     optimising for balanced element exposure (local + global).
                     Optional exchange polish improves D-efficiency post-greedy.
StackBuilder       — orchestrates R respondent blocks into the full N×P matrix.

When ``skip_vif=True`` (set by the generator when the preflight structural VIF
is well below threshold), all per-row and per-block VIF computations are skipped.
"""
import numpy as np
import logging
from typing import List, Set, Optional, Callable

from .config import DesignConfig
from .diagnostics import compute_max_vif
from .row_generator import generate_random_row, row_violates_incompatible_pairs
from .exceptions import BlockConstructionError, BlockUniquenessError

logger = logging.getLogger("RDE_Generator")

_N_CANDIDATES = 100           # random candidate rows evaluated per slot
_W_VIF_ROW = 0.25             # weight for VIF penalty in row scoring
_POLISH_PASSES = 3            # max exchange polish iterations
_POLISH_BALANCE_TOL = 0.05    # max relative balance degradation during polish


def _d_efficiency(X: np.ndarray) -> float:
    """D-efficiency of a T×P block: |X'X|^(1/P) / T. Returns 0 if singular."""
    T, P = X.shape
    sign, logdet = np.linalg.slogdet(X.T @ X)
    if sign <= 0:
        return 0.0
    return np.exp(logdet / P) / T


class RespondentBuilder:
    """Constructs a single T×P block optimising for balanced exposure."""

    def __init__(self, config: DesignConfig, rng: np.random.Generator,
                 skip_vif: bool = False):
        self.config = config
        self.rng = rng
        self.skip_vif = skip_vif
        self.block_rows: List[np.ndarray] = []
        self._row_hashes: Set[int] = set()
        self.local_exposures = np.zeros(config.total_elements, dtype=int)

    def build(self, global_exposures: np.ndarray) -> np.ndarray:
        T = self.config.tasks_per_respondent

        for t in range(T):
            row = self._find_best_row(global_exposures)
            self._add_row(row)

        if self.config.polish:
            self._polish_block(global_exposures)

        block = np.vstack(self.block_rows)
        self.rng.shuffle(block)
        return block

    def _find_best_row(self, global_base: np.ndarray) -> np.ndarray:
        candidates = []
        scores = []
        P = self.config.total_elements
        need_vif = not self.skip_vif and len(self.block_rows) + 1 >= P

        for _ in range(_N_CANDIDATES):
            row = generate_random_row(self.config, self.rng)
            if hash(row.tobytes()) in self._row_hashes:
                continue
            if row_violates_incompatible_pairs(row, self.config.incompatible_pairs):
                continue

            score = self._score(row, global_base)

            if need_vif:
                block_plus = np.vstack(self.block_rows + [row])
                vif = compute_max_vif(block_plus)
                score += _W_VIF_ROW * (vif if np.isfinite(vif) else 1e6)

            candidates.append(row)
            scores.append(score)

        if not candidates:
            raise BlockConstructionError("No valid candidate rows (all duplicates or incompatible).")

        return candidates[int(np.argmin(scores))]

    def _score(self, row: np.ndarray, global_base: np.ndarray) -> float:
        diff_global = np.abs((global_base + row) - self.config.target_exposure_global_vec)
        diff_local = np.abs((self.local_exposures + row) - self.config.target_exposure_local_vec)
        return (self.config.w_global * np.sum(diff_global)
                + self.config.w_local * np.sum(diff_local))

    def _add_row(self, row: np.ndarray):
        self._row_hashes.add(hash(row.tobytes()))
        self.block_rows.append(row)
        self.local_exposures += row

    # --- Exchange polish ---------------------------------------------------

    def _polish_block(self, global_exposures: np.ndarray):
        """
        Pairwise exchange polish: try replacing each row with a random
        candidate that improves D-efficiency without degrading balance.
        """
        T = len(self.block_rows)
        block = np.vstack(self.block_rows)
        current_d = _d_efficiency(block)
        target = self.config.target_exposure_local_vec
        current_balance = np.sum(np.abs(block.sum(axis=0) - target))

        for _ in range(_POLISH_PASSES):
            improved = False
            for i in range(T):
                old_row = block[i].copy()
                best_row = old_row
                best_d = current_d

                # Hashes of all OTHER rows (for duplicate check)
                other_hashes = {hash(block[j].tobytes()) for j in range(T) if j != i}

                for _ in range(_N_CANDIDATES):
                    cand = generate_random_row(self.config, self.rng)
                    if hash(cand.tobytes()) in other_hashes:
                        continue
                    if row_violates_incompatible_pairs(cand, self.config.incompatible_pairs):
                        continue

                    block[i] = cand
                    new_d = _d_efficiency(block)

                    if new_d > best_d:
                        new_balance = np.sum(np.abs(block.sum(axis=0) - target))
                        degradation = (new_balance - current_balance) / max(current_balance, 1.0)
                        if degradation <= _POLISH_BALANCE_TOL:
                            best_row = cand.copy()
                            best_d = new_d

                    block[i] = old_row  # restore for next candidate

                if not np.array_equal(best_row, old_row):
                    block[i] = best_row
                    current_d = best_d
                    current_balance = np.sum(np.abs(block.sum(axis=0) - target))
                    improved = True

            if not improved:
                break

        # Sync internal state
        self.block_rows = [block[i] for i in range(T)]
        self.local_exposures = block.sum(axis=0).astype(int)
        self._row_hashes = {hash(block[i].tobytes()) for i in range(T)}


# ======================================================================

class StackBuilder:
    """Orchestrates construction of the full N×P matrix (one block per respondent)."""

    def __init__(self, config: DesignConfig, rng: np.random.Generator,
                 progress=None, skip_vif: bool = False,
                 progress_callback: Optional[Callable[[int, int], None]] = None):
        self.config = config
        self.rng = rng
        self.progress = progress
        self.skip_vif = skip_vif
        self.progress_callback = progress_callback
        self.global_exposures = np.zeros(config.total_elements, dtype=int)
        self.respondent_blocks: List[np.ndarray] = []
        self.seen_hashes: Set[int] = set()

    def build_stack(self) -> np.ndarray:
        for r in range(self.config.n_respondents):
            self._build_respondent(r)
        return np.vstack(self.respondent_blocks)

    def _build_respondent(self, r_index: int):
        n = self.config.n_respondents
        progress_log = logging.getLogger("IdeaLab")

        for attempt in range(self.config.max_block_retries):
            try:
                builder = RespondentBuilder(self.config, self.rng, skip_vif=self.skip_vif)
                block = builder.build(self.global_exposures)

                # Keep this respondent only when the block can identify every
                # element. A short-rank block is rebuilt; earlier respondents stay.
                block_rank = int(np.linalg.matrix_rank(block))
                if block_rank < self.config.total_elements:
                    raise BlockConstructionError(
                        f"Respondent block rank {block_rank} < {self.config.total_elements}."
                    )

                block_hash = hash(block.tobytes())
                if block_hash in self.seen_hashes:
                    raise BlockUniquenessError("Duplicate block.")

                if not self.skip_vif:
                    X_with = np.vstack(self.respondent_blocks + [block])
                    if compute_max_vif(X_with) > self.config.max_vif:
                        raise BlockConstructionError("Block would exceed max VIF.")

                self.respondent_blocks.append(block)
                self.global_exposures += block.sum(axis=0)
                self.seen_hashes.add(block_hash)
                if self.progress is not None:
                    self.progress[0] = r_index + 1
                if self.progress_callback is not None:
                    try:
                        self.progress_callback(r_index + 1, n)
                    except Exception:
                        pass

                step = max(1, n // 30) if n > 20 else 1
                if (r_index + 1) % step == 0 or r_index == 0 or r_index == n - 1:
                    progress_log.info("Golden Matrix: respondent %d/%d done", r_index + 1, n)
                return

            except (BlockConstructionError, BlockUniquenessError):
                continue

        raise BlockConstructionError(
            f"Failed to build unique block for respondent {r_index + 1} "
            f"after {self.config.max_block_retries} retries."
        )
