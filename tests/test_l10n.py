import pytest

from poketokenbar import l10n


def test_english_is_the_default():
    assert l10n.t("home") == "Home"


@pytest.mark.parametrize("lang,expected", [("ko", "홈"), ("ja", "ホーム"), ("es", "Inicio")])
def test_other_languages_resolve(lang, expected):
    assert l10n.t("home", lang) == expected


def test_unknown_language_falls_back_to_english():
    assert l10n.t("home", "it") == "Home"


def test_unknown_key_returns_the_key_not_blank():
    # A blank label hides the bug; the key makes it visible.
    assert l10n.t("no_such_key", "ko") == "no_such_key"


def test_catalogue_covers_every_string():
    assert set(l10n.catalogue("ja")) == set(l10n.STRINGS)



def test_status_messages_exist_for_each_display_state():
    from poketokenbar.companion import STATUS_MESSAGE

    for kind in STATUS_MESSAGE:
        assert f"status_{kind.lower()}" in l10n.STRINGS or kind == "levelUp"


def test_every_string_has_all_eight_languages():
    assert l10n.LANGUAGES == ("en", "ko", "ja", "es", "fr", "pt", "de", "ru")
    for key, row in l10n.STRINGS.items():
        assert len(row) == 8 and all(row), key


def test_new_languages_resolve():
    assert l10n.t("next_evolution", "pt") == "próxima evolução"  # upstream #406
    assert l10n.t("status_idle", "de") == "Ist heute ganz ruhig."
    assert l10n.t("egg", "ru") == "Яйцо"
    assert l10n.t("shop", "fr") == "Boutique"
