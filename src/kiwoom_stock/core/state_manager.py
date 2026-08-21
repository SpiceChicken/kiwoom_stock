"""Validated physical-state hydration and durable transition orchestration."""

from datetime import datetime
import logging
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from kiwoom_stock.application.ports import PhysicalStateRepository
from kiwoom_stock.core.physics_engine import calculate_net_velocity
from kiwoom_stock.domain.models import (
    PhysicalContinuityEvidence,
    PhysicalObservation,
)
from kiwoom_stock.domain.state import (
    PHYSICAL_TRACKER_SCHEMA_VERSION,
    PhysicalStateBatchCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateValidationError,
    PhysicalStateWrite,
    PhysicalTrackerState,
    calculate_elapsed_hours,
    calculate_initial_velocity_from_rsi,
    calculate_interval_impulse,
    calculate_recovered_velocity,
    calculate_reference_mass,
    calculate_volume_interval,
    calculate_volume_window,
    is_new_volume_session,
    validate_decay_constant,
)

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


def _system_now() -> datetime:
    return datetime.now().astimezone()


def _require_aware(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PhysicalStateValidationError(f"{name} must be timezone-aware")
    return value


class PhysicalStateTracker:
    """Advance memory only after the typed repository acknowledges commit."""

    def __init__(
        self,
        state_repository: PhysicalStateRepository,
        clock: Optional[Clock] = None,
    ) -> None:
        if not isinstance(state_repository, PhysicalStateRepository):
            raise TypeError("state_repository must implement PhysicalStateRepository")
        self._states: Dict[str, PhysicalTrackerState] = {}
        self._hydration_sources: Dict[str, PhysicalStateHydrationSource] = {}
        self.state_repository = state_repository
        self._clock = clock or _system_now

    @property
    def _l1_cache(self) -> Dict[str, float]:
        """Deprecated read-only velocity view for callers still migrating."""

        return {code: state.velocity for code, state in self._states.items()}

    @property
    def _vol_history(self) -> Dict[str, list[float]]:
        """Deprecated read-only volume-history view."""

        return {
            code: list(state.interval_volume_history)
            for code, state in self._states.items()
        }

    def current_state(self, stock_code: str) -> Optional[PhysicalTrackerState]:
        """Return the current committed in-memory state for deterministic evidence."""

        return self._states.get(stock_code)

    def load_or_initialize(
        self,
        stock_code: str,
        decay_constant: float = 0.5,
    ) -> PhysicalTrackerState:
        """Hydrate one complete v1 snapshot or create an explicit cold start."""

        if not isinstance(stock_code, str) or not stock_code:
            raise PhysicalStateValidationError("physical tracker stock_code is required")
        validate_decay_constant(decay_constant)
        current_state = self._states.get(stock_code)
        if current_state is not None:
            return current_state

        now = _require_aware(self._clock(), "physical tracker clock")
        state, source = self._hydrate_staged(stock_code, now, decay_constant)
        next_states = dict(self._states)
        next_sources = dict(self._hydration_sources)
        next_states[stock_code] = state
        next_sources[stock_code] = source
        self._states, self._hydration_sources = next_states, next_sources
        return state

    def _hydrate_staged(
        self,
        stock_code: str,
        now: datetime,
        decay_constant: float = 0.5,
    ) -> Tuple[PhysicalTrackerState, PhysicalStateHydrationSource]:
        """Load and recover one state without publishing tracker memory."""

        validate_decay_constant(decay_constant)
        loaded = self.state_repository.load_physical_state(stock_code)
        if loaded.source is not PhysicalStateHydrationSource.PERSISTED:
            state = PhysicalTrackerState.initial(stock_code, now)
        else:
            if loaded.state is None:
                raise PhysicalStateValidationError("persisted hydration omitted state")
            persisted = loaded.state
            persisted.assert_persistable()
            if persisted.stock_code != stock_code:
                raise PhysicalStateValidationError("physical tracker stock_code mismatch")
            if persisted.updated_at > now:
                raise PhysicalStateValidationError("physical tracker update timestamp is future")
            assert persisted.last_observed_at is not None
            if persisted.last_observed_at > now:
                raise PhysicalStateValidationError("physical tracker observation timestamp is future")
            state = PhysicalTrackerState(
                schema_version=PHYSICAL_TRACKER_SCHEMA_VERSION,
                stock_code=stock_code,
                velocity=calculate_recovered_velocity(
                    persisted.velocity,
                    persisted.updated_at,
                    now,
                    decay_constant,
                ),
                last_cumulative_volume=persisted.last_cumulative_volume,
                last_price=persisted.last_price,
                interval_volume_history=persisted.interval_volume_history,
                last_observed_at=persisted.last_observed_at,
                updated_at=now,
            )
        return state, loaded.source

    def recover_state_from_crash(self, stock_code: str, decay_constant: float = 0.5) -> None:
        """Compatibility wrapper for the public hydration boundary."""

        state = self.load_or_initialize(stock_code, decay_constant)
        elapsed = calculate_elapsed_hours(state.updated_at, self._clock())
        logger.info("[%s] hydrated V:%.2f (%.2fh elapsed)", stock_code, state.velocity, elapsed)

    def process_observation(self, observation: PhysicalObservation) -> Dict[str, Any]:
        """Compatibility batch-of-one physical-state transition."""

        return self.process_observations((observation,))[observation.stock_code]

    def process_observations(
        self,
        observations: Sequence[PhysicalObservation],
    ) -> Dict[str, Dict[str, Any]]:
        """Commit one complete transition batch before publishing any memory."""

        if not isinstance(observations, Sequence) or isinstance(
            observations,
            (str, bytes, bytearray),
        ):
            raise TypeError("observations must be a sequence")
        immutable_observations = tuple(observations)
        if not immutable_observations:
            raise PhysicalStateValidationError("physical observation batch is empty")
        if any(
            not isinstance(observation, PhysicalObservation)
            for observation in immutable_observations
        ):
            raise TypeError("observation must be PhysicalObservation")
        stock_codes = [observation.stock_code for observation in immutable_observations]
        if len(stock_codes) != len(set(stock_codes)):
            raise PhysicalStateValidationError(
                "physical observation stock codes must be unique"
            )
        observed_at_values = {
            observation.observed_at for observation in immutable_observations
        }
        if len(observed_at_values) != 1:
            raise PhysicalStateValidationError(
                "physical observation batch generation is inconsistent"
            )
        now = _require_aware(self._clock(), "physical tracker clock")
        if any(observation.observed_at > now for observation in immutable_observations):
            raise PhysicalStateValidationError("physical observation timestamp is future")

        staged_states: Dict[str, PhysicalTrackerState] = {}
        staged_sources: Dict[str, PhysicalStateHydrationSource] = {}
        staged_results: Dict[str, Dict[str, Any]] = {}
        writes = []
        for observation in immutable_observations:
            prior = self._states.get(observation.stock_code)
            if prior is None:
                prior, hydration_source = self._hydrate_staged(
                    observation.stock_code,
                    now,
                )
            else:
                existing_source = self._hydration_sources.get(
                    observation.stock_code
                )
                if existing_source is None:
                    raise PhysicalStateValidationError(
                        "physical tracker hydration source is unavailable"
                    )
                hydration_source = existing_source
            next_state, forces, continuity = self._prepare_transition(
                observation,
                prior,
                hydration_source,
                now,
            )
            staged_states[observation.stock_code] = next_state
            staged_sources[observation.stock_code] = hydration_source
            staged_results[observation.stock_code] = {
                "forces": forces,
                "continuity": continuity,
            }
            writes.append(
                PhysicalStateWrite(next_state, tuple(forces.items()))
            )

        receipt = self.state_repository.persist_physical_state_batch(tuple(writes))
        self._validate_batch_receipt(
            receipt,
            immutable_observations,
            now,
        )
        next_states = dict(self._states)
        next_sources = dict(self._hydration_sources)
        next_states.update(staged_states)
        next_sources.update(staged_sources)
        self._states, self._hydration_sources = next_states, next_sources
        return staged_results

    def _prepare_transition(
        self,
        observation: PhysicalObservation,
        prior: PhysicalTrackerState,
        hydration_source: PhysicalStateHydrationSource,
        now: datetime,
    ) -> Tuple[PhysicalTrackerState, Dict[str, float], PhysicalContinuityEvidence]:
        """Calculate one transition without persistence or memory mutation."""

        if (
            prior.last_observed_at is not None
            and observation.observed_at <= prior.last_observed_at
        ):
            raise PhysicalStateValidationError("physical observation timestamp did not advance")

        volume_session_reset = is_new_volume_session(
            prior.last_observed_at,
            observation.observed_at,
        )
        last_volume = (
            -1.0
            if volume_session_reset or prior.last_cumulative_volume is None
            else prior.last_cumulative_volume
        )
        volume_interval = calculate_volume_interval(
            last_volume,
            observation.cumulative_volume,
        )
        volume_history = () if volume_session_reset else prior.interval_volume_history
        volume_window = calculate_volume_window(
            volume_history,
            volume_interval.interval_volume,
            volume_interval.is_frozen,
        )
        previous_price = prior.last_price or observation.current_price
        reference_mass = calculate_reference_mass(observation.market_cap)
        impulse = calculate_interval_impulse(
            interval_volume=volume_interval.interval_volume,
            current_price=observation.current_price,
            reference_mass=reference_mass,
            is_frozen=volume_interval.is_frozen,
        )
        effective_strength = 0.0 if volume_interval.is_frozen else observation.strength
        effective_vol_ratio = 0.0 if volume_interval.is_frozen else observation.vol_ratio
        effective_baseline = (
            0.0 if volume_interval.is_frozen else observation.prev_strength_5m
        )
        previous_velocity = prior.velocity
        if prior.last_observed_at is None:
            previous_velocity = calculate_initial_velocity_from_rsi(observation.rsi)

        forces = calculate_net_velocity(
            strength=effective_strength,
            current_price=observation.current_price,
            vwap=observation.vwap,
            atr_percent=observation.atr_percent,
            previous_velocity=previous_velocity,
            vol_ratio=effective_vol_ratio,
            rsi=observation.rsi,
            tot_sel_req=observation.tot_sel_req,
            tot_buy_req=observation.tot_buy_req,
            prev_strength_5m=effective_baseline,
            previous_price=previous_price,
            interval_impulse=impulse.interval_impulse,
            interval_amount_krw=impulse.interval_amount_krw,
            reference_mass=reference_mass,
        )
        forces["volume_drop_ratio"] = volume_window.drop_ratio
        next_state = PhysicalTrackerState(
            schema_version=PHYSICAL_TRACKER_SCHEMA_VERSION,
            stock_code=observation.stock_code,
            velocity=forces["current_velocity"],
            last_cumulative_volume=observation.cumulative_volume,
            last_price=observation.current_price,
            interval_volume_history=volume_window.history,
            last_observed_at=observation.observed_at,
            updated_at=now,
        )
        previous_observed_at = prior.last_observed_at
        continuity = PhysicalContinuityEvidence(
            schema_version=next_state.schema_version,
            hydration_source=hydration_source.value,
            previous_observed_at=previous_observed_at,
            history_depth=len(next_state.interval_volume_history),
            baseline_source=observation.baseline_source,
            baseline_sample_index=observation.baseline_sample_index,
            baseline_time_estimated=observation.baseline_time_estimated,
        )
        return next_state, forces, continuity

    @staticmethod
    def _validate_batch_receipt(
        receipt: PhysicalStateBatchCommitReceipt,
        observations: Tuple[PhysicalObservation, ...],
        committed_at: datetime,
    ) -> None:
        if not isinstance(receipt, PhysicalStateBatchCommitReceipt):
            raise PhysicalStateValidationError(
                "repository returned an invalid batch commit receipt"
            )
        expected_generation = observations[0].observed_at.isoformat()
        expected_codes = tuple(observation.stock_code for observation in observations)
        receipt_codes = tuple(item.stock_code for item in receipt.items)
        if (
            receipt.generation != expected_generation
            or receipt.committed_at != committed_at
            or receipt_codes != expected_codes
            or any(item.generation != expected_generation for item in receipt.items)
        ):
            raise PhysicalStateValidationError(
                "batch commit receipt does not match transitions"
            )

    def process_tick(
        self,
        stock_code: str,
        strength: float,
        current_price: float,
        vwap: float,
        atr_percent: float,
        vol_ratio: float,
        rsi: float,
        tot_sel_req: float,
        tot_buy_req: float,
        total_volume: float = 0.0,
        market_cap: float = 1_000_000_000_000.0,
        *,
        prev_strength_5m: Optional[float] = None,
        observed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Compatibility constructor for callers migrating to PhysicalObservation."""

        cycle_at = observed_at or self._clock()
        return self.process_observation(
            PhysicalObservation(
                stock_code=stock_code,
                observed_at=cycle_at,
                current_price=current_price,
                cumulative_volume=total_volume,
                strength=strength,
                prev_strength_5m=(strength if prev_strength_5m is None else prev_strength_5m),
                vwap=vwap,
                atr_percent=atr_percent,
                vol_ratio=vol_ratio,
                rsi=rsi,
                tot_sel_req=tot_sel_req,
                tot_buy_req=tot_buy_req,
                market_cap=market_cap,
            )
        )
