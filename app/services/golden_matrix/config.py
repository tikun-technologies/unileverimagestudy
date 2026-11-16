import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import logging
import string

from .exceptions import InfeasibleConfigError, RowConstraintsError

logger = logging.getLogger("RDE_Generator")


def _silo_letter(idx: int) -> str:
    """Return A, B, ..., Z, AA, AB, ... for category index."""
    letters = string.ascii_uppercase
    if idx < 26:
        return letters[idx]
    return letters[idx // 26 - 1] + letters[idx % 26]


@dataclass
class DesignConfig:
    """
    Configuration for RDE Design Generation (Individual Level).

    Silos (categories) group mutually-exclusive elements.  Each row
    activates between ``min_actives_per_row`` and ``max_actives_per_row``
    silos, picking exactly one element from each active silo.
    """
    # --- Inputs ---
    n_respondents: int              # R
    tasks_per_respondent: int       # T (0 = auto-compute for comfortable T/P ratio)
    n_categories: int               # C
    elements_per_category: List[int]# E
    min_actives_per_row: int
    max_actives_per_row: int

    # --- Optional silo / element names ---
    category_names: Optional[List[str]] = None
    element_names: Optional[List[List[str]]] = None

    # --- Prior weights (per-element uncertainty) ---
    # Higher weight = more uncertain = gets more exposure.
    # Length must equal total_elements. Default None = uniform.
    element_priors: Optional[List[float]] = None

    # --- Optimization ---
    w_global: float = 0.1
    w_local: float = 0.9
    polish: bool = True             # exchange polishing after greedy build

    # --- QC Thresholds ---
    max_attempts: int = 3
    max_block_retries: int = 1000
    max_vif: float = 10.0
    verbose: bool = False

    # --- Incompatible pairs: (col_idx_a, col_idx_b) that cannot both be 1 ---
    incompatible_pairs: List[Tuple[int, int]] = field(default_factory=list)

    # --- Derived (computed in __post_init__) ---
    total_elements: int = field(init=False)         # P
    total_rows: int = field(init=False)             # N = R * T
    avg_actives: float = field(init=False)
    target_exposure_global: float = field(init=False)   # scalar (base, uniform)
    target_exposure_local: float = field(init=False)    # scalar (base, uniform)
    target_exposure_global_vec: np.ndarray = field(init=False)  # P-length (prior-weighted)
    target_exposure_local_vec: np.ndarray = field(init=False)   # P-length (prior-weighted)
    prior_weights: np.ndarray = field(init=False)       # P-length, mean=1.0
    category_map: Dict[int, List[int]] = field(init=False)
    column_labels: List[str] = field(init=False)

    def __post_init__(self):
        self._validate_inputs()
        self._compute_derived()
        self._build_column_labels()
        self._validate_feasibility()

    def _validate_inputs(self):
        if len(self.elements_per_category) != self.n_categories:
            raise ValueError(
                f"elements_per_category length ({len(self.elements_per_category)}) "
                f"!= n_categories ({self.n_categories})."
            )
        if any(e < 1 for e in self.elements_per_category):
            raise ValueError("Every category must have at least 1 element.")
        if self.n_respondents < 1:
            raise ValueError("n_respondents must be >= 1.")
        if self.tasks_per_respondent < 0:
            raise ValueError("tasks_per_respondent must be >= 0 (0 = auto).")
        if self.min_actives_per_row < 1:
            raise RowConstraintsError("min_actives_per_row must be >= 1.")
        if self.min_actives_per_row > self.max_actives_per_row:
            raise RowConstraintsError("min_actives cannot be > max_actives.")
        if self.max_actives_per_row > self.n_categories:
            raise RowConstraintsError("max_actives cannot exceed n_categories.")
        if self.element_priors is not None:
            P = sum(self.elements_per_category)
            if len(self.element_priors) != P:
                raise ValueError(
                    f"element_priors length ({len(self.element_priors)}) != total_elements ({P})."
                )
            if any(p <= 0 for p in self.element_priors):
                raise ValueError("All element_priors must be positive.")

    def _compute_derived(self):
        self.total_elements = sum(self.elements_per_category)

        # Auto-compute tasks if set to 0: use ceil(1.5 * P) for comfortable T/P
        if self.tasks_per_respondent == 0:
            import math
            self.tasks_per_respondent = math.ceil(1.5 * self.total_elements)
            logger.info("Auto tasks: T=%d (1.5 x P=%d)", self.tasks_per_respondent, self.total_elements)

        self.total_rows = self.n_respondents * self.tasks_per_respondent
        self.avg_actives = (self.min_actives_per_row + self.max_actives_per_row) / 2.0
        self.target_exposure_global = (self.total_rows * self.avg_actives) / self.total_elements
        self.target_exposure_local = (self.tasks_per_respondent * self.avg_actives) / self.total_elements

        # Prior-weighted target vectors
        if self.element_priors is not None:
            w = np.array(self.element_priors, dtype=float)
            self.prior_weights = w / np.mean(w)
        else:
            self.prior_weights = np.ones(self.total_elements, dtype=float)

        self.target_exposure_local_vec = self.target_exposure_local * self.prior_weights
        self.target_exposure_global_vec = self.target_exposure_global * self.prior_weights

        self.category_map = {}
        col = 0
        for cat_idx, count in enumerate(self.elements_per_category):
            self.category_map[cat_idx] = list(range(col, col + count))
            col += count

    def _build_column_labels(self):
        labels = []
        for cat_idx, count in enumerate(self.elements_per_category):
            prefix = (self.category_names[cat_idx]
                      if self.category_names and cat_idx < len(self.category_names)
                      else _silo_letter(cat_idx))
            for el_idx in range(count):
                if (self.element_names
                        and cat_idx < len(self.element_names)
                        and el_idx < len(self.element_names[cat_idx])):
                    labels.append(f"{prefix}:{self.element_names[cat_idx][el_idx]}")
                else:
                    labels.append(f"{prefix}{el_idx + 1}")
        self.column_labels = labels

    def _validate_feasibility(self):
        if self.tasks_per_respondent < 2:
            raise InfeasibleConfigError("tasks_per_respondent must be >= 2.")
        if self.tasks_per_respondent < self.total_elements:
            raise InfeasibleConfigError(
                f"T={self.tasks_per_respondent} < P={self.total_elements}. "
                "More variables than data points per respondent. Increase T."
            )
        if self.target_exposure_global < 1.0:
            raise InfeasibleConfigError("Infeasible density: k* < 1.0.")
        if self.tasks_per_respondent == self.total_elements:
            logger.warning("Saturated design (T=P=%d): zero degrees of freedom.", self.total_elements)
