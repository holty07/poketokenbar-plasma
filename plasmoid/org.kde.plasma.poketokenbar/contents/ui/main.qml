import QtQuick
import Qt.labs.platform as Platform
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
    id: root

    // Parsed state.json, or null before the first successful read.
    property var appState: null
    property string stateError: ""

    // QML cannot read $XDG_STATE_HOME, so the default location is used. The
    // daemon falls back to the same path, so the two agree unless the user
    // overrides XDG_STATE_HOME — which install.sh does not do.
    readonly property string homeDir:
        Platform.StandardPaths.writableLocation(Platform.StandardPaths.HomeLocation)
            .toString().replace("file://", "")
    readonly property string statePath:
        homeDir + "/.local/state/poketokenbar/state.json"

    // Read via the executable engine, NOT XMLHttpRequest.
    //
    // Qt refuses XHR against file:// URLs unless QML_XHR_ALLOW_FILE_READ=1 is
    // exported into the whole Plasma session. The first version of this file
    // used XHR and failed silently — the panel looked like the daemon was down.
    // `cat` costs ~1ms and needs no session-wide security opt-out.
    Plasma5Support.DataSource {
        id: stateSource
        engine: "executable"
        connectedSources: ["cat " + root.statePath]
        interval: 2000

        onNewData: function(sourceName, data) {
            if (data["exit code"] !== 0) {
                // Distinguish causes — collapsing them is what made the XHR
                // failure look like a daemon outage.
                root.stateError = i18n("Cannot read state file: %1",
                                       (data["stderr"] || "").trim());
                return;
            }
            try {
                root.appState = JSON.parse(data["stdout"]);
                root.stateError = "";
            } catch (e) {
                // Keep the last good appState so a transient torn read does not
                // blank the panel. state.write() is atomic, so this is rare.
                root.stateError = i18n("state.json is not valid JSON");
            }
        }
    }

    // plasma6-only:begin — packaging/prepare-plasma5.sh drops this block:
    // Plasmoid.contextualActions / PlasmaCore.Action are Plasma 6 API.
    Plasma5Support.DataSource {
        id: commandRunner
        engine: "executable"
        connectedSources: []
        onNewData: function(sourceName, data) { disconnectSource(sourceName); }
        function run(cmd) { connectSource(cmd); }
    }

    // Right-click menu on the panel item (upstream #373), next to Plasma's
    // own Configure entry.
    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Refresh now")
            icon.name: "view-refresh"
            onTriggered: commandRunner.run("poketokenctl refresh")
        },
        PlasmaCore.Action {
            readonly property bool petOn: !!(root.appState && root.appState.settings
                                             && root.appState.settings.floating_pet_enabled)
            text: petOn ? i18n("Hide floating pet") : i18n("Show floating pet")
            icon.name: "face-smile"
            onTriggered: commandRunner.run("poketokenctl set floating_pet_enabled " + (petOn ? "false" : "true"))
        }
    ]
    // plasma6-only:end

    compactRepresentation: CompactRepresentation {}
    fullRepresentation: FullRepresentation {}
}
