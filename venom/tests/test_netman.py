"""Wi-Fi manager — a fake nmcli runner exercises parsing, the add/edit/remove
logic and the anti-lockout safety rules, with no NetworkManager present."""

from venom import netman

# nmcli -t -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE,DEVICE
SAVED = "\n".join([
    "MyPhone:802-11-wireless:yes:100:yes:wlan0",
    "Home:802-11-wireless:yes:0:no:",
    r"Cafe\:Free:802-11-wireless:yes:0:no:",   # escaped ':' in the name
    "Wired connection 1:802-3-ethernet:yes:-999:no:",  # not wifi → ignored
])
# nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list
WIFI = "\n".join([
    "*:MyPhone:72:WPA2",
    ":Office:40:WPA2",
    ":Home:55:WPA2",
    "::0:",                # blank SSID (hidden) → skipped
])


class FakeNM:
    """Answers reads from canned output; records mutations, which return rc=0."""

    def __init__(self, saved=SAVED, wifi=WIFI, mutate_rc=0, read_rc=0):
        self.saved, self.wifi = saved, wifi
        self.mutate_rc, self.read_rc = mutate_rc, read_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "show" in argv and "connection" in argv:
            return self.read_rc, self.saved
        if "list" in argv and "wifi" in argv:
            return self.read_rc, self.wifi
        if "rescan" in argv:
            return 0, ""
        return self.mutate_rc, ""

    def issued(self, *needles):
        """True if some recorded mutation contained all `needles` in order-free."""
        return any(all(n in argv for n in needles) for argv in self.calls)


# ── overview / parsing ─────────────────────────────────────────────────────────
def test_overview_parses_saved_and_available():
    o = netman.overview(FakeNM())
    assert o["nm"] is True
    names = [n["name"] for n in o["saved"]]
    assert names[0] == "MyPhone"           # sorted highest-priority first
    assert "Cafe:Free" in names            # escaped colon unescaped
    assert "Wired connection 1" not in names  # ethernet filtered out
    phone = next(n for n in o["saved"] if n["name"] == "MyPhone")
    assert phone["priority"] == 100 and phone["active"] is True


def test_overview_marks_current_and_known():
    o = netman.overview(FakeNM())
    assert o["current"]["name"] == "MyPhone"
    assert o["current"]["signal"] == 72
    known = {a["ssid"]: a["known"] for a in o["available"]}
    assert known["MyPhone"] is True and known["Home"] is True
    assert known["Office"] is False


def test_overview_when_nm_absent():
    o = netman.overview(FakeNM(read_rc=1))
    assert o == {"nm": False, "current": {}, "saved": [], "available": []}


# ── add / update ───────────────────────────────────────────────────────────────
def test_add_new_network_creates_profile():
    nm = FakeNM()
    msg = netman.add_or_update(nm, "Office", "supersecret", priority=5)
    assert "Added Office" in msg
    assert nm.issued("connection", "add", "Office")
    assert nm.issued("wifi-sec.psk", "supersecret")
    assert nm.issued("connection.autoconnect", "yes")
    assert nm.issued("connection.autoconnect-priority", "5")


def test_add_existing_network_modifies_in_place():
    nm = FakeNM()
    msg = netman.add_or_update(nm, "Home", "newpassword")
    assert "Updated Home" in msg
    assert nm.issued("connection", "modify", "Home")
    assert not nm.issued("connection", "add", "Home")


def test_open_network_has_no_psk():
    nm = FakeNM()
    netman.add_or_update(nm, "FreeWifi")
    assert nm.issued("connection", "add", "FreeWifi")
    assert not any("wifi-sec.psk" in a for a in nm.calls)


def test_short_password_rejected():
    nm = FakeNM()
    msg = netman.add_or_update(nm, "Office", "short")
    assert "at least 8" in msg
    assert not nm.issued("connection", "add")


def test_blank_ssid_rejected():
    nm = FakeNM()
    assert "name" in netman.add_or_update(nm, "   ", "whatever8")


# ── remove: never cut our own connection ───────────────────────────────────────
def test_remove_active_network_is_blocked():
    nm = FakeNM()
    msg = netman.remove(nm, "MyPhone")  # MyPhone is the active connection
    assert "connected through" in msg
    assert not nm.issued("connection", "delete")


def test_remove_inactive_network_deletes():
    nm = FakeNM()
    msg = netman.remove(nm, "Home")
    assert "Removed Home" in msg
    assert nm.issued("connection", "delete", "Home")


def test_remove_unknown_network():
    nm = FakeNM()
    assert "No saved network" in netman.remove(nm, "Ghost")


# ── priority / base ────────────────────────────────────────────────────────────
def test_set_base_beats_all_other_priorities():
    nm = FakeNM()
    msg = netman.set_base(nm, "Home")
    assert "base network" in msg
    # MyPhone is 100, so Home's new priority must exceed it (100 + 10).
    assert nm.issued("connection", "modify", "Home",
                     "connection.autoconnect-priority", "110")


def test_set_base_unknown_network():
    nm = FakeNM()
    assert "Add it first" in netman.set_base(nm, "Nope")


def test_connect_brings_profile_up():
    nm = FakeNM()
    assert "Connected to Home" in netman.connect(nm, "Home")
    assert nm.issued("connection", "up", "Home")


def test_mutation_failure_is_reported():
    nm = FakeNM(mutate_rc=1)
    assert "Couldn't" in netman.add_or_update(nm, "Office", "supersecret")
