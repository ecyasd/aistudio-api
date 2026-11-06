from aistudio_api.infrastructure.utils.usage_report import UsageRow, build_report


def test_usage_filters_closed_requests_buckets_missing_tokens_and_previous_period():
    rows = [
        UsageRow(
            id="previous",
            started_at=90,
            model="gemini-test",
            state="completed",
            stream=True,
            timings={"first_body": 100},
        ),
        UsageRow(
            id="a",
            started_at=110,
            model="gemini-test",
            protocol="openai",
            state="completed",
            stream=True,
            timings={"first_body": 0},
            usage={"total_tokens": 12},
        ),
        UsageRow(
            id="b",
            started_at=120,
            model="gemini-test",
            state="failed",
            status=200,
            error_status=429,
        ),
        UsageRow(
            id="c",
            started_at=130,
            model="gemini-test-build",
            channel="assistant",
            state="cancelled",
        ),
        UsageRow(id="active", started_at=140, state="running"),
        UsageRow(id="boundary", started_at=200, state="completed"),
    ]
    report = build_report(rows, start=100, end=200)
    assert report["totals"]["requests"] == 3
    assert report["totals"]["rate_limited"] == 1
    assert report["totals"]["reported_usage"] == 1
    assert report["totals"]["total_tokens"] == 12
    assert report["totals"]["first_body_p50"] == 0
    assert report["previous"]["requests"] == 1
    assert report["buckets"][0]["requests"] == 3
    filtered = build_report(rows, start=100, end=200, channel="assistant")
    assert filtered["totals"]["requests"] == 1
    assert filtered["totals"]["first_body_p50"] is None


def test_usage_pagination_and_model_ranking():
    rows = [
        UsageRow(id=str(i), started_at=i, model="gemini-test", state="completed")
        for i in range(120)
    ]
    report = build_report(rows, start=0, end=120, page=2)
    assert report["pages"] == 3
    assert len(report["records"]) == 50
    assert report["records"][0]["id"] == "69"
    assert report["ranking"][0]["requests"] == 120
