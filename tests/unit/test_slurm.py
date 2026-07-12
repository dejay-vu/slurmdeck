from __future__ import annotations

import pytest

from slurmdeck.models.status import SchedulerSource
from slurmdeck.slurm import (
    expand_array_id,
    failed_states,
    normalize_state,
    parse_sacct,
    parse_sbatch_parsable,
    parse_squeue,
    sacct_command,
    sbatch_command,
    scancel_command,
    squeue_command,
)


class TestParsers:
    def test_normalize_state(self):
        assert normalize_state("CANCELLED by 1234") == "CANCELLED"
        assert normalize_state("COMPLETED+") == "COMPLETED"
        assert normalize_state("") == ""

    def test_expand_array_id(self):
        assert expand_array_id("123_[0-2,5]%4") == ["123_0", "123_1", "123_2", "123_5"]
        assert expand_array_id("123_[0-2,5%4]") == ["123_0", "123_1", "123_2", "123_5"]
        assert expand_array_id("123_[0-4:2]") == ["123_0", "123_2", "123_4"]
        assert expand_array_id("123_7") == ["123_7"]
        assert expand_array_id("123.batch") == []

    def test_parse_squeue_preserves_reason_source_time_and_array_identity(self):
        records = parse_squeue(
            "999_[0-1%48]|PENDING|Priority\n999_2|RUNNING|node01\n999_3|PENDING|(None)\n",
            observed_at=123.5,
        )

        assert records["999_0"].job_id == "999"
        assert records["999_0"].array_task_id == "0"
        assert records["999_0"].scheduler_state == "PENDING"
        assert records["999_0"].scheduler_reason == "Priority"
        assert records["999_0"].observed_at == 123.5
        assert records["999_0"].source is SchedulerSource.SQUEUE
        assert records["999_2"].scheduler_state == "RUNNING"
        assert records["999_2"].scheduler_reason == "node01"
        assert records["999_3"].scheduler_reason == ""

    def test_parse_sacct_preserves_exit_reason_source_time_and_skips_steps(self):
        output = "999_0|FAILED|1:0|None\n999_0.batch|FAILED|1:0|None\n999_1|COMPLETED|0:0|None\n"
        records = parse_sacct(output, observed_at=456.0)

        assert set(records) == {"999_0", "999_1"}
        assert records["999_0"].job_id == "999"
        assert records["999_0"].array_task_id == "0"
        assert records["999_0"].scheduler_state == "FAILED"
        assert records["999_0"].exit_code == "1:0"
        assert records["999_0"].scheduler_reason == ""
        assert records["999_0"].observed_at == 456.0
        assert records["999_0"].source is SchedulerSource.SACCT

    def test_parse_sbatch_parsable(self):
        assert parse_sbatch_parsable("motd noise\n12345;cluster\n") == "12345"
        assert parse_sbatch_parsable("999001\n") == "999001"
        with pytest.raises(ValueError, match="parsable"):
            parse_sbatch_parsable("Submitted batch job nope")

    def test_failed_states_excludes_completed(self):
        assert "COMPLETED" not in failed_states()
        assert "TIMEOUT" in failed_states()


class TestCommandBuilders:
    def test_quoting(self):
        assert sbatch_command("/p a/s.sbatch") == "sbatch --parsable '/p a/s.sbatch'"
        assert squeue_command(["1", "2"]) == "squeue -h -o '%i|%T|%R' -j 1,2"
        assert "1,2" in sacct_command(["1", "2"])
        assert scancel_command(["10", "11"]) == "scancel 10 11"
