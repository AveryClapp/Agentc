"""Post-acquisition transport validity; never silently repair the frozen run."""


def provider_failures(calls):
    # This text-only workflow deliberately permits length stops so cap-induced
    # damage is measured, not selectively removed. Other finish states are not
    # successful text completions. Even stop/length is no quality guarantee.
    return [{"id": row["id"], "finish_reason": row.get("finish_reason"), "arm": row["arm"],
             "task_id": row["task_id"], "workflow_stage": row["workflow_stage"]}
            for row in calls if row.get("finish_reason") not in {"stop", "length"}]
