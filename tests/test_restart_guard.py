"""Restart-storm brake — the counter must survive restarts (file-based),
prune correctly, and the halt flag must be sticky until cleared."""

import time

import pytest

import config
import restart_guard as rg


class TestCounting:
    def test_empty_is_zero(self, tmp_path):
        assert rg.restarts_in_window(log_dir=str(tmp_path)) == 0

    def test_records_accumulate(self, tmp_path):
        for _ in range(2):
            rg.record_restart(log_dir=str(tmp_path))
        assert rg.restarts_in_window(log_dir=str(tmp_path)) == 2

    def test_old_entries_pruned_from_window_and_file(self, tmp_path):
        now = time.time()
        with open(tmp_path / rg.RESTART_LOG, "w") as f:
            f.write(f"{now - config.RESTART_STORM_WINDOW_SEC - 100:.0f}\n")  # stale
            f.write(f"{now - 60:.0f}\n")                                     # fresh
        assert rg.restarts_in_window(now=now, log_dir=str(tmp_path)) == 1
        # pruning persisted — the file cannot grow without bound
        assert len((tmp_path / rg.RESTART_LOG).read_text().split()) == 1

    def test_corrupt_file_counts_zero(self, tmp_path):
        (tmp_path / rg.RESTART_LOG).write_text("not-a-number\n")
        assert rg.restarts_in_window(log_dir=str(tmp_path)) == 0

    def test_storm_threshold_reached(self, tmp_path):
        for _ in range(config.RESTART_STORM_MAX):
            rg.record_restart(log_dir=str(tmp_path))
        assert (rg.restarts_in_window(log_dir=str(tmp_path))
                >= config.RESTART_STORM_MAX)


class TestHaltFlag:
    def test_lifecycle(self, tmp_path):
        d = str(tmp_path)
        assert rg.halt_active(d) is None
        rg.trigger_halt("restart storm: 3 restarts in 60 min", d)
        assert "restart storm" in rg.halt_active(d)
        # sticky across "restarts" (re-reads)
        assert rg.halt_active(d) is not None
        assert rg.clear_halt(d) is True
        assert rg.halt_active(d) is None

    def test_clear_also_resets_counter(self, tmp_path):
        d = str(tmp_path)
        rg.record_restart(log_dir=d)
        rg.trigger_halt("x", d)
        rg.clear_halt(d)
        assert rg.restarts_in_window(log_dir=d) == 0

    def test_empty_flag_still_halts(self, tmp_path):
        (tmp_path / rg.HALT_FLAG).write_text("")
        assert rg.halt_active(str(tmp_path)) is not None
