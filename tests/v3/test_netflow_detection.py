from aiops_v3.netflow_detection import (
    NetflowSettings,
    detect,
    signal_rows,
)


def row(service, city, minute, req, resp):
    return {
        "minute": minute,
        "service": service,
        "service_node": f"{'beida' if service == 'dns' else 'shanghai' if service == 'web' else 'wuhan'}-service-vm-1",
        "observer_city": city,
        "req_pkts": req,
        "resp_pkts": resp,
        "req_bytes": req * 100,
        "resp_bytes": resp * 100,
    }


def healthy(service, minutes, cities=("beida", "xian")):
    return [row(service, city, minute, 500, 500) for city in cities for minute in minutes]


def test_all_observers_low_yields_all_scope():
    rows = healthy("dns", range(0, 10))
    rows += [row("dns", city, minute, 500, 0) for city in ("beida", "xian") for minute in range(10, 16)]
    events = detect(rows, minute_count=100)
    assert len(events) == 1
    event = events[0]
    assert (event.start_minute, event.end_minute, event.scope) == (10, 16, "all")
    assert event.service_node == "beida-service-vm-1"
    assert set(event.affected_cities) == {"beida", "xian"}


def test_single_observer_low_yields_subset_scope():
    rows = healthy("dns", range(0, 10))
    rows += [row("dns", "beida", minute, 500, 0) for minute in range(10, 16)]
    rows += [row("dns", "xian", minute, 500, 500) for minute in range(10, 16)]
    events = detect(rows, minute_count=100)
    assert [(e.start_minute, e.end_minute, e.scope, e.affected_cities) for e in events] == [
        (10, 16, "subset", ("beida",))
    ]


def test_gap_minutes_block_event():
    rows = healthy("dns", range(0, 20))
    for minute in range(10, 16):
        if minute == 12:
            continue
        rows.append(row("dns", "beida", minute, 500, 0))
        rows.append(row("dns", "xian", minute, 500, 0))
    assert detect(rows, minute_count=100) == ()


def test_short_glitch_is_dropped_with_min_minutes_two():
    rows = healthy("dns", range(0, 10))
    rows += [row("dns", city, 10, 500, 0) for city in ("beida", "xian")]
    events = detect(rows, minute_count=100, settings=NetflowSettings(min_minutes=2))
    assert events == ()


def test_long_window_is_split_at_30_minutes():
    rows = healthy("dns", range(0, 40))
    rows += [row("dns", city, minute, 500, 0) for city in ("beida", "xian") for minute in range(40, 100)]
    events = detect(rows, minute_count=200)
    assert [(e.start_minute, e.end_minute) for e in events] == [(40, 70), (70, 100)]


def test_low_request_minutes_are_not_treated_as_fault():
    rows = healthy("dns", range(0, 10))
    rows += [row("dns", city, minute, 10, 0) for city in ("beida", "xian") for minute in range(10, 16)]
    assert detect(rows, minute_count=100) == ()


def test_service_signals_carry_service_category_prior():
    rows = healthy("dns", range(0, 10))
    rows += [row("dns", city, minute, 500, 0) for city in ("beida", "xian") for minute in range(10, 16)]
    event = detect(rows, minute_count=100)[0]
    signals = signal_rows(event)
    assert len(signals) == 1
    assert signals[0]["feature"] == "netflow.dns.response_ratio"
    assert signals[0]["category"] == "service_down"
    assert signals[0]["role"] == "direct"


def test_path_signals_expose_affected_city_router_context():
    rows = healthy("auth", range(0, 10))
    rows += [row("auth", "xian", minute, 500, 0) for minute in range(10, 16)]
    rows += [row("auth", "beida", minute, 500, 500) for minute in range(10, 16)]
    event = detect(rows, minute_count=100)[0]
    signals = signal_rows(event)
    assert signals[0]["node"] == "wuhan-service-vm-1"
    assert signals[0]["role"] == "symptom"
    assert any(item["node"] == "xian-br-1" and item["role"] == "context" for item in signals)
