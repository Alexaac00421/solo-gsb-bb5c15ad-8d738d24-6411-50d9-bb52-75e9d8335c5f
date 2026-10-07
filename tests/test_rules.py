"""运营规则发布与回溯重算测试。"""
import concurrent.futures


def post_trip(client, tid, acc, ts, fare, status=201):
    r = client.post("/trips", json={
        "trip_id": tid, "account_id": acc,
        "boarding_at": ts, "base_fare_cents": fare})
    assert r.status_code == status, r.text
    return r.json()


def publish(client, day, window, cap, expected, status=201):
    r = client.post("/rules", json={
        "effective_day": day, "transfer_window_minutes": window,
        "daily_cap_cents": cap, "expected_version": expected})
    assert r.status_code == status, r.text
    return r.json()


def day_view(client, acc, day):
    r = client.get(f"/accounts/{acc}/days/{day}")
    assert r.status_code == 200, r.text
    return r.json()


def ledger(client, acc):
    return client.get(f"/accounts/{acc}/ledger?limit=200").json()["entries"]


# ---------------------------------------------------------- 发布与适用查询

def test_publish_and_applicable_rule(client):
    body = publish(client, "2026-10-06", 60, 1500, 0)
    assert body["version"] == 1
    assert body["effective_day"] == "2026-10-06"
    assert body["transfer_window_minutes"] == 60
    assert body["daily_cap_cents"] == 1500
    assert body["coverage_start"] == "2026-10-06"
    assert body["coverage_end"] is None          # 尚无下一条规则
    assert body["recalculated_account_days"] == 0

    # 生效日当天及之后适用新规则
    r = client.get("/rules/applicable?op_day=2026-10-06").json()
    assert r["source"] == "rule" and r["version"] == 1
    assert r["transfer_window_minutes"] == 60 and r["daily_cap_cents"] == 1500
    r = client.get("/rules/applicable?op_day=2027-01-01").json()
    assert r["source"] == "rule" and r["version"] == 1

    # 生效日之前沿用默认配置（90 分钟 / 2000 分）
    r = client.get("/rules/applicable?op_day=2026-10-05").json()
    assert r["source"] == "default" and r["version"] == 0
    assert r["transfer_window_minutes"] == 90 and r["daily_cap_cents"] == 2000
    assert r["coverage_end"] == "2026-10-05"     # 默认配置覆盖至首条规则前

    # 规则列表与当前版本
    r = client.get("/rules").json()
    assert r["current_version"] == 1
    assert [x["version"] for x in r["rules"]] == [1]


def test_coverage_intervals_with_multiple_rules(client):
    publish(client, "2026-10-06", 60, 1500, 0)
    body = publish(client, "2026-10-10", 120, 3000, 1)
    assert body["version"] == 2
    assert body["coverage_start"] == "2026-10-10" and body["coverage_end"] is None

    # 前一条规则覆盖至下一生效日前一天
    r = client.get("/rules/applicable?op_day=2026-10-08").json()
    assert r["version"] == 1
    assert r["coverage_start"] == "2026-10-06"
    assert r["coverage_end"] == "2026-10-09"
    r = client.get("/rules/applicable?op_day=2026-10-10").json()
    assert r["version"] == 2 and r["coverage_end"] is None


def test_version_conflict_and_no_data_change(client):
    publish(client, "2026-10-06", 60, 1500, 0)
    # 重复 0 版本 / 跳跃版本都被拒绝，且不改动数据
    r = client.post("/rules", json={
        "effective_day": "2026-10-07", "transfer_window_minutes": 30,
        "daily_cap_cents": 100, "expected_version": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"
    r = client.post("/rules", json={
        "effective_day": "2026-10-07", "transfer_window_minutes": 30,
        "daily_cap_cents": 100, "expected_version": 5})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"

    assert client.get("/rules").json()["current_version"] == 1
    r = client.get("/rules/applicable?op_day=2026-10-07").json()
    assert r["version"] == 1 and r["transfer_window_minutes"] == 60


def test_duplicate_effective_day_rejected(client):
    publish(client, "2026-10-06", 60, 1500, 0)
    r = client.post("/rules", json={
        "effective_day": "2026-10-06", "transfer_window_minutes": 30,
        "daily_cap_cents": 100, "expected_version": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RULE_DAY_CONFLICT"
    # 数据未被改动
    r = client.get("/rules/applicable?op_day=2026-10-06").json()
    assert r["transfer_window_minutes"] == 60 and r["daily_cap_cents"] == 1500


def test_invalid_rule_inputs(client):
    def post(body):
        return client.post("/rules", json=body)

    base = {"effective_day": "2026-10-06", "transfer_window_minutes": 60,
            "daily_cap_cents": 1500, "expected_version": 0}

    r = post(dict(base, effective_day="not-a-date"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"
    assert post(dict(base, effective_day="2026-13-01")).status_code == 422
    r = post(dict(base, effective_day="1999-01-01"))   # 年份越界
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"

    r = post(dict(base, transfer_window_minutes=0))
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_WINDOW"
    assert post(dict(base, transfer_window_minutes=-5)).status_code == 422
    assert post(dict(base, transfer_window_minutes=1.5)).status_code == 422

    r = post(dict(base, daily_cap_cents=-1))
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CAP"
    assert post(dict(base, daily_cap_cents="abc")).status_code == 422

    r = post(dict(base, expected_version=-1))
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_VERSION"

    assert post({k: v for k, v in base.items() if k != "daily_cap_cents"}
                ).status_code == 422                      # 缺字段
    assert post(dict(base, extra=1)).status_code == 422   # 多余字段
    # 全部拒绝后库里仍无规则
    assert client.get("/rules").json()["current_version"] == 0


def test_applicable_rule_invalid_date(client):
    r = client.get("/rules/applicable?op_day=not-a-date")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_DATE"


# ---------------------------------------------------------- 回溯重算

def test_window_narrowing_recalcs_existing_days(client, account):
    # 08:00(200) 与 09:20(500) 间隔 80 分钟：默认 90 分钟窗口下同组，实收 500
    post_trip(client, "w1", account, "2026-10-06T08:00:00+08:00", 200)
    post_trip(client, "w2", account, "2026-10-06T09:20:00+08:00", 500)
    d = day_view(client, account, "2026-10-06")
    assert len(d["groups"]) == 1 and d["charged_cents"] == 500

    # 回溯发布 60 分钟窗口 -> 拆成两组，实收 700
    body = publish(client, "2026-10-06", 60, 2000, 0)
    assert body["recalculated_account_days"] == 1

    d = day_view(client, account, "2026-10-06")
    assert len(d["groups"]) == 2
    assert d["sum_group_fare_cents"] == 700
    assert d["daily_cap_cents"] == 2000
    assert d["charged_cents"] == 700

    # 实收变化沿用冲销 + 记入，reason 指向规则发布
    entries = ledger(client, account)
    rev = [e for e in entries if e["event_type"] == "day_reversal"][-1]
    chg = [e for e in entries if e["event_type"] == "day_charge"][-1]
    assert rev["delta_cents"] == -500 and "rule_published:v1" in rev["reason"]
    assert chg["delta_cents"] == 700 and chg["charged_after_cents"] == 700


def test_cap_change_recalcs_existing_days(client, account):
    # 3 个独立组 × 800 = 2400，默认封顶 2000
    for i, ts in enumerate(["06:00:00", "10:00:00", "14:00:00"]):
        post_trip(client, f"c{i}", account,
                  f"2026-10-06T{ts}+08:00", 800)
    d = day_view(client, account, "2026-10-06")
    assert d["sum_group_fare_cents"] == 2400 and d["charged_cents"] == 2000

    publish(client, "2026-10-06", 90, 2200, 0)
    d = day_view(client, account, "2026-10-06")
    assert d["sum_group_fare_cents"] == 2400
    assert d["daily_cap_cents"] == 2200
    assert d["charged_cents"] == 2200

    entries = ledger(client, account)
    rev = [e for e in entries if e["event_type"] == "day_reversal"][-1]
    chg = [e for e in entries if e["event_type"] == "day_charge"][-1]
    assert rev["delta_cents"] == -2000 and chg["delta_cents"] == 2200


def test_no_money_ledger_when_recalc_unchanged(client, account):
    post_trip(client, "n1", account, "2026-10-06T08:00:00+08:00", 200)
    before = ledger(client, account)
    # 窗口收窄不影响单乘次分组，封顶不变 -> 实收不变，不产生金额流水
    body = publish(client, "2026-10-06", 60, 2000, 0)
    assert body["recalculated_account_days"] == 1
    after = ledger(client, account)
    money_before = [e for e in before
                    if e["event_type"] in ("day_reversal", "day_charge")]
    money_after = [e for e in after
                   if e["event_type"] in ("day_reversal", "day_charge")]
    assert money_before == money_after
    assert day_view(client, account, "2026-10-06")["charged_cents"] == 200


def test_backdated_publish_only_recalcs_coverage(client, account):
    post_trip(client, "b0", account, "2026-10-05T08:00:00+08:00", 800)
    post_trip(client, "b1", account, "2026-10-06T08:00:00+08:00", 200)
    post_trip(client, "b2", account, "2026-10-06T09:20:00+08:00", 500)
    post_trip(client, "b3", account, "2026-10-07T08:00:00+08:00", 100)
    before5 = [e for e in ledger(client, account) if e["op_day"] == "2026-10-05"]

    # 规则自 10-06 生效：10-06 与 10-07 重算，10-05 不受影响
    body = publish(client, "2026-10-06", 60, 2000, 0)
    assert body["recalculated_account_days"] == 2

    d5 = day_view(client, account, "2026-10-05")
    assert d5["charged_cents"] == 800 and d5["daily_cap_cents"] == 2000
    after5 = [e for e in ledger(client, account) if e["op_day"] == "2026-10-05"]
    assert before5 == after5                       # 覆盖区间外不产生新流水
    assert day_view(client, account, "2026-10-06")["charged_cents"] == 700
    assert day_view(client, account, "2026-10-07")["daily_cap_cents"] == 2000


def test_multiple_rules_each_day_uses_its_own(client, account):
    # 10-06 与 10-09 各两程，间隔 80 分钟
    for day in ("2026-10-06", "2026-10-09"):
        post_trip(client, f"m{day}-a", account, f"{day}T08:00:00+08:00", 200)
        post_trip(client, f"m{day}-b", account, f"{day}T09:20:00+08:00", 500)

    publish(client, "2026-10-06", 60, 2000, 0)    # v1：10-06 起窗口 60
    body = publish(client, "2026-10-08", 120, 2000, 1)  # v2：10-08 起窗口 120
    assert body["coverage_start"] == "2026-10-08"
    # v2 的覆盖区间从 10-08 开始，只重算 10-09 这一天
    assert body["recalculated_account_days"] == 1

    # 10-06 适用 v1（窗口 60 -> 拆组 700）；10-09 适用 v2（窗口 120 -> 同组 500）
    assert day_view(client, account, "2026-10-06")["charged_cents"] == 700
    assert day_view(client, account, "2026-10-09")["charged_cents"] == 500
    r = client.get("/rules/applicable?op_day=2026-10-07").json()
    assert r["version"] == 1
    r = client.get("/rules/applicable?op_day=2026-10-09").json()
    assert r["version"] == 2


def test_zero_cap_allowed(client, account):
    post_trip(client, "z1", account, "2026-10-06T08:00:00+08:00", 800)
    publish(client, "2026-10-06", 90, 0, 0)       # 封顶 0：非负整数的下界
    d = day_view(client, account, "2026-10-06")
    assert d["sum_group_fare_cents"] == 800
    assert d["daily_cap_cents"] == 0 and d["charged_cents"] == 0
    entries = ledger(client, account)
    rev = [e for e in entries if e["event_type"] == "day_reversal"][-1]
    assert rev["delta_cents"] == -800
    # 新实收为 0：只有冲销，没有新收
    assert [e for e in entries if e["event_type"] == "day_charge"
            and "rule_published" in e["reason"]] == []


def test_new_trips_use_applicable_rule(client, account):
    publish(client, "2026-10-06", 30, 1500, 0)
    # 间隔 40 分钟：10-05（默认 90）同组；10-06（规则 30）拆组
    post_trip(client, "p1", account, "2026-10-05T08:00:00+08:00", 200)
    post_trip(client, "p2", account, "2026-10-05T08:40:00+08:00", 500)
    post_trip(client, "p3", account, "2026-10-06T08:00:00+08:00", 200)
    post_trip(client, "p4", account, "2026-10-06T08:40:00+08:00", 500)
    d5 = day_view(client, account, "2026-10-05")
    d6 = day_view(client, account, "2026-10-06")
    assert len(d5["groups"]) == 1 and d5["charged_cents"] == 500
    assert len(d6["groups"]) == 2 and d6["charged_cents"] == 700
    assert d6["daily_cap_cents"] == 1500


def test_rule_published_event_in_operator_ledger(client):
    publish(client, "2026-10-06", 60, 1500, 0)
    entries = client.get("/accounts/operator/ledger?limit=50").json()["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["event_type"] == "rule_published"
    assert e["op_day"] == "2026-10-06"
    assert e["delta_cents"] == 0 and e["charged_after_cents"] is None
    assert "v1" in e["reason"]


# ---------------------------------------------------------- 并发与一致性

def test_concurrent_publish_and_submits_serialized(client, account):
    # 预先 4 程（间隔 60 分钟，默认窗口下同组，实收 100）
    for i in range(4):
        post_trip(client, f"pre{i}", account,
                  f"2026-10-06T{8 + i:02d}:00:00+08:00", 100)

    def submit(i):
        return client.post("/trips", json={
            "trip_id": f"post{i}", "account_id": account,
            "boarding_at": f"2026-10-06T{12 + i:02d}:00:00+08:00",
            "base_fare_cents": 200})

    def do_publish():
        return client.post("/rules", json={
            "effective_day": "2026-10-06", "transfer_window_minutes": 30,
            "daily_cap_cents": 2000, "expected_version": 0})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(submit, i) for i in range(4)]
        futures.append(ex.submit(do_publish))
        results = [f.result() for f in futures]
    assert all(r.status_code in (200, 201) for r in results)

    # 无论交错顺序如何，终态确定：8 程在 30 分钟窗口下各自成组
    d = day_view(client, account, "2026-10-06")
    assert len(d["groups"]) == 8
    assert d["sum_group_fare_cents"] == 4 * 100 + 4 * 200
    assert d["charged_cents"] == 1200

    # 流水全局 seq 连续无空洞（发布事件在 operator 账户下）
    seqs = sorted(e["seq"] for e in ledger(client, account)
                  + ledger(client, "operator"))
    assert seqs == list(range(1, len(seqs) + 1))
