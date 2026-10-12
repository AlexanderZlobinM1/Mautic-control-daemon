from mcd_agent.daemon import _remote_runtime_response_is_stale


class Store:
    def __init__(self, value):
        self.value = value

    def get_runtime_sync(self, key):
        return self.value if key == "local_runtime_accepted" else None


def test_new_host_revision_does_not_reject_lagging_instance_scope():
    store = Store({"host_revision": 145, "instance_revisions": {"adonis": 166}})
    response = {
        "desired_state": {"revision": 160},
        "instance_desired_states": {"adonis": {"revision": 159}, "other": {"revision": 170}},
    }
    assert _remote_runtime_response_is_stale(store, response) is False


def test_older_host_revision_is_rejected_even_with_new_instance_scope():
    store = Store({"host_revision": 160, "instance_revisions": {"adonis": 166}})
    response = {
        "desired_state": {"revision": 159},
        "instance_desired_states": {"adonis": {"revision": 170}},
    }
    assert _remote_runtime_response_is_stale(store, response) is True
