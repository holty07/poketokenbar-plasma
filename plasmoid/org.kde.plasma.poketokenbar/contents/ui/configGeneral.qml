import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.plasma5support as Plasma5Support

// SimpleKCM, not a bare FormLayout: Plasma's config dialog does not scroll a
// plain layout, so everything past the fold (pet size, backup) was unreachable.
KCM.SimpleKCM {
    id: page

    // Plasma injects a cfg_<key>Default for every cfg_<key> alias, plus a
    // `title`. Without them the whole page fails to construct and the settings
    // dialog comes up empty — which is exactly what happened.
    property alias cfg_showTokens: showTokens.checked
    property bool cfg_showTokensDefault: false
    property alias cfg_showCost: showCost.checked
    property bool cfg_showCostDefault: false
    property string title: ""

    property var settings: ({})
    property var snapshots: []
    readonly property var folderProviders: ["claude_code", "codex", "antigravity", "cursor",
                                            "kiro", "pi", "omp", "kimi_code", "aside"]
    readonly property var folderRows: {
        var map = page.settings.extra_scan_folders || {};
        var rows = [];
        for (var pid in map)
            for (var i = 0; i < map[pid].length; i++)
                rows.push({ provider: pid, path: map[pid][i] });
        return rows;
    }


    function push(key, value) {
        var text = (typeof value === "boolean") ? (value ? "true" : "false") : String(value);
        runner.run("poketokenctl set " + key + " " + text);
    }

    // Read current values so the controls open showing reality rather than
    // Plasma's separate copy of the defaults.
    function reload() {
        runner.read("cat $HOME/.config/poketokenbar/config.json");
        runner.readState();
    }

    Component.onCompleted: reload()

    Plasma5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []

        property string pending: ""
        readonly property string stateCmd: "cat $HOME/.local/state/poketokenbar/state.json"

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            if (sourceName === stateCmd && data["exit code"] === 0) {
                try {
                    page.snapshots = JSON.parse(data["stdout"]).snapshots || [];
                } catch (e) {
                    page.snapshots = [];
                }
                return;
            }
            if (sourceName === pending && data["exit code"] === 0) {
                try {
                    page.settings = JSON.parse(data["stdout"]);
                    page.applySettings();
                } catch (e) {
                    // No config yet: the daemon writes one on first change.
                }
            }
        }

        function run(cmd) { connectSource(cmd); }
        function read(cmd) { pending = cmd; connectSource(cmd); }
        function readState() { connectSource(stateCmd); }
    }

    function applySettings() {
        var s = page.settings;
        if (s.refresh_interval !== undefined)
            refreshInterval.value = s.refresh_interval;
        if (s.warn_threshold !== undefined)
            warnThreshold.value = s.warn_threshold;
        if (s.crit_threshold !== undefined)
            critThreshold.value = s.crit_threshold;
        if (s.show_tokens_in_menu !== undefined)
            showTokens.checked = s.show_tokens_in_menu;
        if (s.show_cost_in_menu !== undefined)
            showCost.checked = s.show_cost_in_menu;
        if (s.show_limit_in_menu !== undefined)
            showLimit.checked = s.show_limit_in_menu;
        if (s.companion_notifications !== undefined)
            companionAlerts.checked = s.companion_notifications;
        if (s.status_checks_enabled !== undefined)
            statusChecks.checked = s.status_checks_enabled;
        if (s.floating_pet_enabled !== undefined)
            floatingPet.checked = s.floating_pet_enabled;
        if (s.floating_pet_size !== undefined)
            petSize.value = s.floating_pet_size;
        if (s.floating_pet_bubble_alerts !== undefined)
            petBubbles.checked = s.floating_pet_bubble_alerts;
        if (s.language !== undefined)
            language.currentIndex = language.keys.indexOf(s.language);
        if (s.growth_difficulty !== undefined)
            growthDifficulty.value = page.difficultyPosition(s.growth_difficulty);
        if (s.shop_difficulty !== undefined)
            shopDifficulty.value = page.difficultyPosition(s.shop_difficulty);
    }

    // Difficulty sliders are logarithmic over 0.1x..2x (upstream #244): the
    // same ratio covers the same distance, and 1x snaps within 1% of the track.
    readonly property real difficultyMin: 0.1
    readonly property real difficultyMax: 2.0
    function difficultyAt(position) {
        var p = Math.min(Math.max(position, 0), 1);
        if (Math.abs(p - page.difficultyPosition(1.0)) < 0.01)
            return 1.0;
        var v = page.difficultyMin * Math.pow(page.difficultyMax / page.difficultyMin, p);
        return Math.round(v * 100) / 100;
    }
    function difficultyPosition(value) {
        var v = Math.min(Math.max(Number(value) || 1.0, page.difficultyMin), page.difficultyMax);
        return Math.log(v / page.difficultyMin) / Math.log(page.difficultyMax / page.difficultyMin);
    }

    Kirigami.FormLayout {

    // ---------------- General ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("General") }

    QQC2.ComboBox {
        id: language
        Kirigami.FormData.label: i18n("Language:")
        readonly property var keys: ["en", "ko", "ja", "es", "fr", "pt", "de", "ru"]
        model: ["English", "한국어", "日本語", "Español", "Français", "Português (Brasil)",
                "Deutsch", "Русский"]
        onActivated: page.push("language", keys[currentIndex])
    }

    QQC2.SpinBox {
        id: refreshInterval
        Kirigami.FormData.label: i18n("Refresh interval (s):")
        from: 30
        to: 3600
        stepSize: 30
        value: 120
        onValueModified: page.push("refresh_interval", value)
    }

    // ---------------- Panel ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Show in panel") }

    QQC2.CheckBox {
        id: showTokens
        Kirigami.FormData.label: i18n("Today's tokens:")
        onToggled: page.push("show_tokens_in_menu", checked)
    }

    QQC2.CheckBox {
        id: showCost
        Kirigami.FormData.label: i18n("Today's cost:")
        onToggled: page.push("show_cost_in_menu", checked)
    }

    QQC2.CheckBox {
        id: showLimit
        Kirigami.FormData.label: i18n("Evolution / graduation progress:")
        onToggled: page.push("show_limit_in_menu", checked)
    }

    QQC2.Label {
        text: i18n("Off shows only the character")
        opacity: 0.7
    }

    // ---------------- Difficulty ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Difficulty") }

    RowLayout {
        Kirigami.FormData.label: i18n("Growth:")
        // Labelled ends (upstream #398): which way is easier is not obvious.
        QQC2.Label { text: i18n("Faster"); opacity: 0.7 }
        QQC2.Slider {
            id: growthDifficulty
            from: 0; to: 1; value: page.difficultyPosition(1.0)
            Layout.preferredWidth: Kirigami.Units.gridUnit * 8
            // Saved on release: a change rescales banked progress, so it is
            // applied once rather than on every pixel of the drag.
            onPressedChanged: if (!pressed) page.push("growth_difficulty", page.difficultyAt(value))
        }
        QQC2.Label { text: i18n("Slower"); opacity: 0.7 }
        QQC2.Label { text: page.difficultyAt(growthDifficulty.value).toFixed(2) + "×" }
    }

    RowLayout {
        Kirigami.FormData.label: i18n("Shop prices:")
        QQC2.Label { text: i18n("Cheaper"); opacity: 0.7 }
        QQC2.Slider {
            id: shopDifficulty
            from: 0; to: 1; value: page.difficultyPosition(1.0)
            Layout.preferredWidth: Kirigami.Units.gridUnit * 8
            onPressedChanged: if (!pressed) page.push("shop_difficulty", page.difficultyAt(value))
        }
        QQC2.Label { text: i18n("Pricier"); opacity: 0.7 }
        QQC2.Label { text: page.difficultyAt(shopDifficulty.value).toFixed(2) + "×" }
    }

    QQC2.Label {
        text: i18n("Changing growth keeps the progress you've earned as a fraction — it never hatches or evolves by itself.")
        opacity: 0.7
        wrapMode: Text.Wrap
        Layout.maximumWidth: Kirigami.Units.gridUnit * 20
    }

    // ---------------- Notifications ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Notifications") }

    RowLayout {
        Kirigami.FormData.label: i18n("Warning:")
        QQC2.Slider {
            id: warnThreshold
            from: 50; to: 99; stepSize: 1; value: 80
            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
            onMoved: page.push("warn_threshold", Math.round(value))
        }
        QQC2.Label { text: Math.round(warnThreshold.value) + "%" }
    }

    RowLayout {
        Kirigami.FormData.label: i18n("Critical:")
        QQC2.Slider {
            id: critThreshold
            from: 60; to: 100; stepSize: 1; value: 95
            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
            onMoved: page.push("crit_threshold", Math.round(value))
        }
        QQC2.Label { text: Math.round(critThreshold.value) + "%" }
    }

    QQC2.CheckBox {
        id: companionAlerts
        Kirigami.FormData.label: i18n("Companion events:")
        text: i18n("hatch / evolve / graduate")
        onToggled: page.push("companion_notifications", checked)
    }

    QQC2.CheckBox {
        id: statusChecks
        Kirigami.FormData.label: i18n("Provider status:")
        text: i18n("show Claude / OpenAI incidents")
        onToggled: page.push("status_checks_enabled", checked)
    }

    // ---------------- Floating pet ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Floating pet") }

    QQC2.CheckBox {
        id: floatingPet
        Kirigami.FormData.label: i18n("Show floating pet:")
        text: i18n("add the desktop widget separately")
        onToggled: page.push("floating_pet_enabled", checked)
    }

    RowLayout {
        Kirigami.FormData.label: i18n("Size:")
        QQC2.Slider {
            id: petSize
            from: 48; to: 192; stepSize: 8; value: 96
            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
            onMoved: page.push("floating_pet_size", Math.round(value))
        }
        QQC2.Label { text: Math.round(petSize.value) + "px" }
    }

    QQC2.CheckBox {
        id: petBubbles
        Kirigami.FormData.label: i18n("Speech bubbles:")
        onToggled: page.push("floating_pet_bubble_alerts", checked)
    }

    // ---------------- Scan folders ----------------
    // Extra folders per provider (upstream #177), on top of the built-in
    // locations — for logs synced from another machine or a custom path.

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Extra scan folders") }

    Repeater {
        model: page.folderRows
        RowLayout {
            Kirigami.FormData.label: index === 0 ? i18n("Scanning:") : ""
            QQC2.Label { text: modelData.provider; font.bold: true }
            QQC2.Label {
                text: modelData.path
                elide: Text.ElideMiddle
                Layout.maximumWidth: Kirigami.Units.gridUnit * 14
            }
            QQC2.Button {
                icon.name: "list-remove"
                text: i18n("Remove")
                display: QQC2.AbstractButton.IconOnly
                onClicked: {
                    runner.run("poketokenctl folder remove " + modelData.provider + " '"
                               + modelData.path.replace(/'/g, "'\\''") + "'");
                    folderReload.restart();
                }
            }
        }
    }

    RowLayout {
        Kirigami.FormData.label: i18n("Add folder:")
        QQC2.ComboBox {
            id: folderProvider
            model: page.folderProviders
        }
        QQC2.TextField {
            id: folderPath
            placeholderText: i18n("/path/to/logs")
            Layout.preferredWidth: Kirigami.Units.gridUnit * 12
        }
        QQC2.Button {
            text: i18n("Add")
            enabled: folderPath.text.trim().length > 0
            onClicked: {
                runner.run("poketokenctl folder add " + page.folderProviders[folderProvider.currentIndex]
                           + " '" + folderPath.text.trim().replace(/'/g, "'\\''") + "'");
                folderPath.text = "";
                folderReload.restart();
            }
        }
    }

    Timer {
        id: folderReload
        interval: 800
        onTriggered: page.reload()
    }

    // ---------------- Backup ----------------

    Item { Kirigami.FormData.isSection: true; Kirigami.FormData.label: i18n("Backup & transfer") }

    RowLayout {
        Kirigami.FormData.label: i18n("Save file:")

        QQC2.Button {
            text: i18n("Export…")
            onClicked: page.push_export()
        }

        QQC2.Button {
            text: i18n("Import…")
            onClicked: page.push_import()
        }
    }

    QQC2.Label {
        text: i18n("Exports to ~/poketokenbar-save.json — Pokédex, tokens, bag, and companion")
        opacity: 0.7
        wrapMode: Text.Wrap
    }

    // Local snapshots (upstream #330): taken automatically every 12 hours,
    // the newest ten kept, and used to recover a corrupt save.
    RowLayout {
        Kirigami.FormData.label: i18n("Backups:")

        QQC2.Button {
            text: i18n("Back up now")
            onClicked: {
                runner.run("poketokenctl snapshot");
                snapshotRefresh.restart();
            }
        }
    }

    RowLayout {
        Kirigami.FormData.label: i18n("Restore:")
        enabled: page.snapshots.length > 0

        QQC2.ComboBox {
            id: snapshotPicker
            Layout.preferredWidth: Kirigami.Units.gridUnit * 14
            model: page.snapshots.map(function (snap) {
                return i18n("%1 — %2 in Pokédex", snap.date_text, snap.dex_count);
            })
        }

        QQC2.Button {
            id: restoreButton
            property bool armed: false
            text: armed ? i18n("Confirm restore") : i18n("Restore")
            onClicked: {
                if (!armed) {
                    armed = true;
                    restoreDisarm.restart();
                    return;
                }
                armed = false;
                var snap = page.snapshots[snapshotPicker.currentIndex];
                if (snap)
                    runner.run("poketokenctl restore " + snap.id);
                snapshotRefresh.restart();
            }
            Timer { id: restoreDisarm; interval: 6000; onTriggered: restoreButton.armed = false }
        }
    }

    QQC2.Label {
        text: page.snapshots.length > 0
              ? i18n("Restoring replaces your progress; the current save is backed up first.")
              : i18n("No backups yet — one is taken automatically every 12 hours.")
        opacity: 0.7
        wrapMode: Text.Wrap
        Layout.maximumWidth: Kirigami.Units.gridUnit * 20
    }

    Timer {
        id: snapshotRefresh
        interval: 4000
        onTriggered: runner.readState()
    }

    }

    function push_export() {
        runner.run("poketokenctl export $HOME/poketokenbar-save.json");
    }

    function push_import() {
        runner.run("poketokenctl import $HOME/poketokenbar-save.json");
    }
}
