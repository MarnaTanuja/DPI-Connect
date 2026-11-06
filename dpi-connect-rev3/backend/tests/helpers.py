ADMIN_KEY = "test-admin-key"


def admin_headers():
    return {"X-API-Key": ADMIN_KEY}


def admin_decide(client, conn_id, approve=True, notes="reviewed in test"):
    return client.post(
        f"/api/admin/connections/{conn_id}/decision", headers=admin_headers(),
        json={"decision": "approve" if approve else "reject", "reviewer_name": "Test Admin", "notes": notes},
    )
