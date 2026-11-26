from app.permissions import campaigns_only_path_allowed, is_campaigns_only, norm_role


class _User:
    def __init__(self, role: str):
        self.role = role


def test_norm_role():
    assert norm_role("Media Designer") == "media_designer"
    assert norm_role("campaigns_only") == "campaigns_only"


def test_is_campaigns_only():
    assert is_campaigns_only(_User("media_designer"))
    assert is_campaigns_only(_User("Campaigns_Only"))
    assert not is_campaigns_only(_User("admin"))


def test_campaigns_only_api_paths():
    assert campaigns_only_path_allowed("/api/campaigns/media")
    assert campaigns_only_path_allowed("/api/campaigns/media/1/file")
    assert campaigns_only_path_allowed("/api/me")
    assert campaigns_only_path_allowed("/api/auth/logout")
    assert campaigns_only_path_allowed("/api/whatsapp/media/55dd3b220d10427fa1b7014bc345b980.pdf")
    assert not campaigns_only_path_allowed("/api/leads")
    assert not campaigns_only_path_allowed("/api/dashboard")
