import QtQuick
import org.kde.kirigami as Kirigami

// PlasmaComponents.ProgressBar inherits the desktop color scheme, which can
// make the fill nearly invisible against some panel/Plasma themes. This bar
// draws its own track and fill in fixed Catppuccin colors so it stays legible
// no matter what theme the user runs.
Item {
    id: bar

    property real from: 0
    property real to: 1
    property real value: 0
    property color fillColor: "#74c7ec"
    property color trackColor: "#313244"

    implicitHeight: Kirigami.Units.gridUnit * 0.4

    readonly property real ratio: bar.to > bar.from
        ? Math.max(0, Math.min(1, (bar.value - bar.from) / (bar.to - bar.from)))
        : 0

    Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: bar.trackColor
    }

    Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        radius: height / 2
        width: track.width * bar.ratio
        color: bar.fillColor
        visible: bar.ratio > 0

        Behavior on width {
            NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
        }
    }
}
