from venom.monitors.audio import find_usb_audio, list_audio_cards, parse_asound_cards

PI_WITH_HEADSET = """\
 0 [Headphones     ]: bcm2835_headphonbcm2835 Headphones - bcm2835 Headphones
                      bcm2835 Headphones
 1 [Device         ]: USB-Audio - USB PnP Sound Device
                      C-Media Electronics Inc. USB PnP Sound Device at usb-0000:01:00.0-1.3
"""

PI_NO_HEADSET = """\
 0 [Headphones     ]: bcm2835_headphonbcm2835 Headphones - bcm2835 Headphones
                      bcm2835 Headphones
"""


def test_parse_cards():
    cards = parse_asound_cards(PI_WITH_HEADSET)
    assert len(cards) == 2
    assert cards[0].index == 0 and cards[0].id == "Headphones"
    assert cards[1].index == 1 and cards[1].is_usb


def test_find_usb_headset(tmp_path):
    cards_file = tmp_path / "cards"
    cards_file.write_text(PI_WITH_HEADSET, encoding="utf-8")
    headset = find_usb_audio(cards_file)
    assert headset is not None
    assert headset.index == 1
    assert "USB" in headset.description


def test_no_usb_headset(tmp_path):
    cards_file = tmp_path / "cards"
    cards_file.write_text(PI_NO_HEADSET, encoding="utf-8")
    assert find_usb_audio(cards_file) is None


def test_missing_alsa_returns_none(tmp_path):
    assert list_audio_cards(tmp_path / "does-not-exist") is None
    assert find_usb_audio(tmp_path / "does-not-exist") is None


def test_empty_cards_file(tmp_path):
    cards_file = tmp_path / "cards"
    cards_file.write_text("--- no soundcards ---\n", encoding="utf-8")
    assert list_audio_cards(cards_file) == []
