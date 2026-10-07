import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami

// Stacked bars of token usage, one bar per bucket (a day or a month), one
// segment per provider (upstream #270/#348/#332). Hovering a bar shows its
// exact usage (#368). Colours come from colorFor(providerId) so a provider
// keeps its colour across charts and periods.
Item {
    id: chart

    // [{ label, tip, segments: [{ id, tokens, cost }], total, cost, muted }]
    property var buckets: []
    property var colorFor: function (pid) { return "grey"; }
    property var nameFor: function (pid) { return pid; }
    property var formatTokens: function (n) { return String(n); }
    property var formatCost: function (v) { return "$" + v.toFixed(2); }
    property color surface: "#1e1e2e"
    property color axisColor: "#45475a"
    // Label every nth bucket under the axis so labels never collide.
    property int labelEvery: 1

    readonly property real maxTotal: {
        var m = 0;
        for (var i = 0; i < chart.buckets.length; i++)
            m = Math.max(m, chart.buckets[i].total);
        return m;
    }

    implicitHeight: Kirigami.Units.gridUnit * 6

    ColumnLayout {
        anchors.fill: parent
        spacing: 2

        Item {
            id: plot
            Layout.fillWidth: true
            Layout.fillHeight: true

            // Recessive baseline.
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: chart.axisColor
            }

            Row {
                id: bars
                anchors.fill: parent
                spacing: chart.buckets.length > 20 ? 1 : 3

                Repeater {
                    model: chart.buckets

                    Item {
                        id: barSlot
                        readonly property var bucket: modelData
                        width: chart.buckets.length > 0
                               ? (bars.width - bars.spacing * (chart.buckets.length - 1)) / chart.buckets.length
                               : 0
                        height: bars.height

                        // Segments stack from the baseline; a 2px surface gap
                        // separates neighbours.
                        Column {
                            id: stack
                            anchors.bottom: parent.bottom
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: Math.min(parent.width, Kirigami.Units.gridUnit)
                            spacing: 2
                            opacity: barSlot.bucket.muted ? 0.35 : 1.0

                            Repeater {
                                // Reverse so the first provider sits on the baseline.
                                model: barSlot.bucket.segments.slice().reverse()

                                Rectangle {
                                    width: stack.width
                                    height: chart.maxTotal > 0
                                            ? Math.max(1, (plot.height - 4) * modelData.tokens / chart.maxTotal - 2)
                                            : 0
                                    color: chart.colorFor(modelData.id)
                                    // Rounded data-end on the top segment only.
                                    radius: index === 0 ? Math.min(2, width / 2) : 0
                                }
                            }
                        }

                        // Hit target is the whole column, wider than the mark.
                        MouseArea {
                            id: hover
                            anchors.fill: parent
                            hoverEnabled: true
                        }

                        Rectangle {
                            // Hover highlight behind the bar.
                            anchors.fill: parent
                            z: -1
                            visible: hover.containsMouse && barSlot.bucket.total > 0
                            color: chart.axisColor
                            opacity: 0.35
                            radius: 2
                        }

                        QQC2.ToolTip.visible: hover.containsMouse && barSlot.bucket.total > 0
                        QQC2.ToolTip.delay: 150
                        QQC2.ToolTip.text: {
                            var b = barSlot.bucket;
                            var lines = [b.tip + " — " + chart.formatTokens(b.total)
                                         + " · " + chart.formatCost(b.cost)];
                            for (var i = 0; i < b.segments.length; i++) {
                                var s = b.segments[i];
                                lines.push(chart.nameFor(s.id) + ": " + chart.formatTokens(s.tokens));
                            }
                            return lines.join("\n");
                        }
                    }
                }
            }
        }

        // Sparse axis labels in muted ink, never in a series colour.
        Row {
            Layout.fillWidth: true
            spacing: bars.spacing

            Repeater {
                model: chart.buckets

                PlasmaComponents.Label {
                    width: chart.buckets.length > 0
                           ? (bars.width - bars.spacing * (chart.buckets.length - 1)) / chart.buckets.length
                           : 0
                    horizontalAlignment: Text.AlignHCenter
                    text: index % chart.labelEvery === 0 ? modelData.label : ""
                    opacity: 0.6
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    clip: false
                }
            }
        }
    }
}
