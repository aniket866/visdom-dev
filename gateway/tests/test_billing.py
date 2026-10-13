# Copyright 2017-present, The Visdom Authors
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.models import User
from app.routers import billing as billing_router
from app.routers import workspaces as workspaces_router

BILLING = "/api/v1/billing"
WORKSPACES = "/api/v1/workspaces"


def test_list_plans(client):
    response = client.get(f"{BILLING}/plans")
    assert response.status_code == 200
    plans = response.json()
    assert [plan["id"] for plan in plans] == ["free", "pro", "enterprise"]

    free, pro, enterprise = plans
    assert free["price"] == 0
    assert free["limits"] == {"workspaces": 1, "members": 3, "api_keys": 2}
    assert pro["price"] == 29
    assert pro["limits"]["members"] is None
    assert enterprise["price"] is None
    assert enterprise["limits"] == {"workspaces": None, "members": None, "api_keys": None}
    assert all(plan["features"] for plan in plans)


def test_subscription_requires_auth(client):
    assert client.get(f"{BILLING}/subscription").status_code == 401
    assert client.post(f"{BILLING}/subscription", json={"tier": "pro"}).status_code == 401


def test_fresh_user_subscription(client, make_user):
    user = make_user()
    response = client.get(f"{BILLING}/subscription", headers=user["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["tier"] == "free"
    assert data["plan"]["id"] == "free"
    assert data["usage"] == {
        "workspaces": {"used": 0, "limit": 1},
        "members": {"used": 0, "limit": 3},
        "api_keys": {"used": 0, "limit": 2},
    }


def test_subscription_usage_counts(client, make_user, make_workspace, add_member):
    owner = make_user()
    member = make_user()
    pending = make_user()
    ws1 = make_workspace(owner)
    make_workspace(owner)
    add_member(owner, ws1, member)
    client.post(
        f"/api/v1/workspaces/{ws1['id']}/members",
        json={"email": pending["email"], "role": "member"},
        headers=owner["headers"],
    )
    client.post("/api/v1/keys", json={"name": "usage-key"}, headers=owner["headers"])

    usage = client.get(f"{BILLING}/subscription", headers=owner["headers"]).json()["usage"]
    assert usage["workspaces"]["used"] == 2
    assert usage["members"]["used"] == 1
    assert usage["api_keys"]["used"] == 1

    member_usage = client.get(f"{BILLING}/subscription", headers=member["headers"]).json()["usage"]
    assert member_usage["workspaces"]["used"] == 0
    assert member_usage["members"]["used"] == 0
    assert member_usage["api_keys"]["used"] == 0


def test_paid_plan_changes_require_a_verified_entitlement(client, make_user, db_session):
    user = make_user()

    for tier in ("pro", "enterprise"):
        response = client.post(
            f"{BILLING}/subscription", json={"tier": tier}, headers=user["headers"]
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Paid plan changes require a verified subscription."

    assert client.get(f"{BILLING}/subscription", headers=user["headers"]).json()["tier"] == "free"
    first_workspace = client.post(
        WORKSPACES,
        json={"name": "First", "slug": "first-free-workspace"},
        headers=user["headers"],
    )
    assert first_workspace.status_code == 201
    second_workspace = client.post(
        WORKSPACES,
        json={"name": "Second", "slug": "second-free-workspace"},
        headers=user["headers"],
    )
    assert second_workspace.status_code == 403

    # Simulate a paid tier granted by a trusted subscription integration.
    account = db_session.query(User).filter(User.email == user["email"]).one()
    account.tier = "pro"
    db_session.commit()
    downgraded = client.post(
        f"{BILLING}/subscription", json={"tier": "free"}, headers=user["headers"]
    )
    assert downgraded.status_code == 200
    assert downgraded.json()["tier"] == "free"
    assert client.get("/api/v1/auth/me", headers=user["headers"]).json()["tier"] == "free"


def test_workspace_creation_uses_tier_after_a_concurrent_downgrade(
    file_database_client, monkeypatch
):
    client, file_sessions = file_database_client
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "quota-downgrade@example.com", "password": "securepassword"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        data={"username": "quota-downgrade@example.com", "password": "securepassword"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    db = file_sessions()
    account = db.query(User).filter(User.email == "quota-downgrade@example.com").one()
    account.tier = "pro"
    db.commit()
    db.close()

    for number in range(2):
        created = client.post(
            WORKSPACES,
            json={"name": f"Existing {number}", "slug": f"existing-{number}"},
            headers=headers,
        )
        assert created.status_code == 201

    original_lock = workspaces_router.lock_user_for_workspace_quota
    downgrade_has_lock = Event()
    allow_downgrade_to_finish = Event()
    creation_is_waiting_for_lock = Event()

    def hold_downgrade_lock(db, user_id):
        user = original_lock(db, user_id)
        downgrade_has_lock.set()
        assert allow_downgrade_to_finish.wait(timeout=10)
        return user

    def signal_creation_lock(db, user_id):
        creation_is_waiting_for_lock.set()
        return original_lock(db, user_id)

    monkeypatch.setattr(billing_router, "lock_user_for_workspace_quota", hold_downgrade_lock)
    monkeypatch.setattr(workspaces_router, "lock_user_for_workspace_quota", signal_creation_lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        downgrade = executor.submit(
            client.post,
            f"{BILLING}/subscription",
            json={"tier": "free"},
            headers=headers,
        )
        assert downgrade_has_lock.wait(timeout=5)
        create = executor.submit(
            client.post,
            WORKSPACES,
            json={"name": "Must be rejected", "slug": "over-limit"},
            headers=headers,
        )
        try:
            assert creation_is_waiting_for_lock.wait(timeout=5)
        finally:
            allow_downgrade_to_finish.set()

        assert downgrade.result(timeout=10).status_code == 200
        assert create.result(timeout=10).status_code == 403


def test_change_plan_invalid_tier(client, make_user):
    user = make_user()
    response = client.post(
        f"{BILLING}/subscription", json={"tier": "platinum"}, headers=user["headers"]
    )
    assert response.status_code == 422
