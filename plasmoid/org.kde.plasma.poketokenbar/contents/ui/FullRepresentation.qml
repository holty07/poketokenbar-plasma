import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami

PlasmaExtras.Representation {
    id: full

    readonly property var today: root.appState ? root.appState.today : null
    readonly property var providers: root.appState ? root.appState.providers : ({})
    readonly property var periods: root.appState && root.appState.periods ? root.appState.periods : null
    readonly property var limits: root.appState && root.appState.limits ? root.appState.limits : null
    // Official Claude usage limits are disabled for now (see daemon.py) — the
    // section below stays hidden rather than showing an empty heading.
    readonly property bool hasLimits: !!(full.limits && (full.limits.session || full.limits.weekly))
    readonly property var companion: root.appState && root.appState.companion
                                     && root.appState.companion.stage
                                     ? root.appState.companion : null
    readonly property var shopItems: root.appState && root.appState.shop ? root.appState.shop : []
    readonly property var bagItems: root.appState && root.appState.bag ? root.appState.bag : []
    readonly property var dexItems: root.appState && root.appState.dex ? root.appState.dex : []
    readonly property var catchLog: root.appState && root.appState.catch_log ? root.appState.catch_log : []
    readonly property var rarityCounts: root.appState && root.appState.rarity_counts
                                        ? root.appState.rarity_counts : ({})
    readonly property var catchCounts: root.appState && root.appState.catch_counts
                                       ? root.appState.catch_counts : ({})
    // The Pokedex counts species, the catch log counts individuals — 14
    // catches can be 28 species, so the filter pills cannot share a tally.
    readonly property var activeCounts: collectionTabs.currentIndex === 1
                                        ? catchCounts : rarityCounts
    readonly property var burn: root.appState && root.appState.burn ? root.appState.burn : ({})
    readonly property var strings: root.appState && root.appState.strings
                                   ? root.appState.strings : ({})
    readonly property var celebration: root.appState && root.appState.celebration
                                       && root.appState.celebration.kind
                                       ? root.appState.celebration : null
    property string lastUpdatedText: ""
    readonly property var providerStatus: root.appState && root.appState.provider_status
                                          ? root.appState.provider_status : ({})
    // The daemon writes updated_at every poll. If it stops, the numbers freeze
    // while still looking authoritative — so say so rather than lying quietly.
    readonly property bool stale: root.appState && root.appState.updated_at
                                  ? (Date.now() / 1000 - root.appState.updated_at) > 600
                                  : false

    property string rarityFilter: ""
    property int dexPage: 0

    // Per-item UI state lives here, not in the delegates: state.json is
    // re-read every 2 s and the Repeaters rebuild their delegates each time,
    // which would reset a quantity stepper or a half-finished confirmation.
    property var quantities: ({})        // shop/bag key -> chosen count
    property int selectedSpecies: -1     // Pokédex entry open in the detail panel
    readonly property var selectedEntry: {
        for (var i = 0; i < full.dexItems.length; i++)
            if (full.dexItems[i].species_id === full.selectedSpecies)
                return full.dexItems[i];
        return null;
    }
    readonly property var selectedProfile: full.selectedEntry && full.selectedEntry.profile
                                           ? full.selectedEntry.profile : null
    property string confirmKey: ""       // egg awaiting confirmation
    property int confirmStage: 0         // 1 = release?, 2 = precious release?

    // Main-series type colours (upstream #391).
    readonly property var typeColors: ({
        normal: "#a8a77a", fire: "#ee8130", water: "#6390f0", electric: "#f7d02c",
        grass: "#7ac74c", ice: "#96d9d6", fighting: "#c22e28", poison: "#a33ea1",
        ground: "#e2bf65", flying: "#a98ff3", psychic: "#f95587", bug: "#a6b91a",
        rock: "#b6a136", ghost: "#735797", dragon: "#6f35fc", dark: "#705746",
        steel: "#b7b7ce", fairy: "#d685ad"
    })
    readonly property var statLabels: ({
        "hp": "HP", "attack": "Atk", "defense": "Def",
        "special-attack": "SpA", "special-defense": "SpD", "speed": "Spe"
    })
    function genderSymbol(g) {
        return g === "male" ? "♂" : (g === "female" ? "♀" : "");
    }

    function quantity(key, max) {
        var q = full.quantities[key] || 1;
        return Math.max(1, Math.min(q, Math.max(1, max)));
    }
    function setQuantity(key, value) {
        var next = Object.assign({}, full.quantities);
        next[key] = value;
        full.quantities = next;
    }
    function resetConfirm() {
        full.confirmKey = "";
        full.confirmStage = 0;
    }
    function buyEgg(key) {
        var releasing = full.companion && full.companion.stage === "mon";
        var stage = full.confirmKey === key ? full.confirmStage : 0;
        if (!releasing) {
            runner.run("poketokenctl buy " + key);
            full.resetConfirm();
        } else if (stage === 0) {
            full.confirmKey = key;
            full.confirmStage = 1;
            confirmTimeout.restart();
        } else if (stage === 1 && full.companion.high_value) {
            // A legendary or shiny gets a second, explicit step (#335).
            full.confirmStage = 2;
            confirmTimeout.restart();
        } else {
            runner.run("poketokenctl buy " + key + (stage === 2 ? " --confirm" : ""));
            full.resetConfirm();
        }
    }

    Timer {
        id: confirmTimeout
        interval: 6000
        onTriggered: full.resetConfirm()
    }
    readonly property int dexPageSize: 24

    // Catppuccin Mocha — used for progress bars, badges, and status colors so
    // they stay legible regardless of the desktop's own color scheme.
    readonly property color ctpBase: "#1e1e2e"
    readonly property color ctpSurface0: "#313244"
    readonly property color ctpSurface1: "#45475a"
    readonly property color ctpOverlay0: "#6c7086"
    readonly property color ctpText: "#cdd6f4"
    readonly property color ctpRed: "#f38ba8"
    readonly property color ctpPeach: "#fab387"
    readonly property color ctpYellow: "#f9e2af"
    readonly property color ctpGreen: "#a6e3a1"
    readonly property color ctpTeal: "#94e2d5"
    readonly property color ctpSapphire: "#74c7ec"
    readonly property color ctpBlue: "#89b4fa"
    readonly property color ctpMauve: "#cba6f7"

    Layout.minimumWidth: Kirigami.Units.gridUnit * 24
    Layout.minimumHeight: Kirigami.Units.gridUnit * 28

    function levelColor(pct) {
        if (pct >= 95)
            return full.ctpRed;
        if (pct >= 80)
            return full.ctpPeach;
        return full.ctpGreen;
    }

    function rarityColor(r) {
        if (r === "legendary")
            return full.ctpYellow;
        if (r === "rare")
            return full.ctpBlue;
        if (r === "uncommon")
            return full.ctpGreen;
        return full.ctpOverlay0;
    }

    function resetIn(iso) {
        if (!iso)
            return "";
        var ms = new Date(iso).getTime() - Date.now();
        if (ms <= 0)
            return i18n("resetting now");
        var mins = Math.floor(ms / 60000);
        var hours = Math.floor(mins / 60);
        var days = Math.floor(hours / 24);
        if (days > 0)
            return i18n("resets in %1d %2h", days, hours % 24);
        if (hours > 0)
            return i18n("resets in %1h %2m", hours, mins % 60);
        return i18n("resets in %1m", mins);
    }

    function grouped(n) {
        return n ? n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",") : "0";
    }

    function compact(n) {
        if (!n)
            return "0";
        // Same bands as poketokenbar/format.py: promote at the rounding
        // boundary so 999,950 reads "1M", not "1000K".
        if (n >= 999950000)
            return (n / 1e9).toFixed(2).replace(/\.?0+$/, "") + "B";
        if (n >= 999950)
            return (n / 1e6).toFixed(1).replace(/\.?0+$/, "") + "M";
        if (n >= 1e3)
            return (n / 1e3).toFixed(1).replace(/\.?0+$/, "") + "K";
        return n.toString();
    }

    function money(v) {
        return "$" + (v ? v.toFixed(2) : "0.00").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    // ---- usage history: month trend and recap (upstream #270/#332/#348/#367/#368/#395) ----
    readonly property var usageHistory: root.appState && root.appState.history ? root.appState.history : ({})
    // Categorical slots in a fixed order; colour follows the provider, never
    // its rank. Validated (dataviz validator) against the Mocha base surface:
    // adjacent CVD ΔE ≥ 8.4, normal-vision ΔE ≥ 19.3, all ≥ 3:1.
    readonly property var providerSlots: ["claude_code", "codex", "opencode", "antigravity",
                                          "cursor", "hermes", "kiro", "pi"]
    readonly property var slotColors: ["#3987e5", "#d95926", "#199e70", "#c98500",
                                       "#d55181", "#008300", "#9085e9", "#e66767"]
    readonly property var providerNames: ({
        claude_code: "Claude Code", codex: "Codex", opencode: "opencode",
        antigravity: "Antigravity", cursor: "Cursor", hermes: "Hermes", kiro: "Kiro",
        pi: "Pi", omp: "omp", aside: "Aside", kimi_code: "Kimi Code", other: i18n("Other")
    })
    property string recapPeriod: "month"   // "week" | "month" | "year"
    property int recapOffset: 0            // 0 = current period, -1 = the one before...

    // Anything past the eight slots folds into "Other" rather than a new hue.
    function seriesId(pid) {
        return full.providerSlots.indexOf(pid) >= 0 ? pid : "other";
    }
    function providerColor(pid) {
        var i = full.providerSlots.indexOf(pid);
        return i >= 0 ? full.slotColors[i] : full.ctpOverlay0;
    }
    function providerName(pid) {
        return full.providerNames[pid] || pid;
    }
    function seriesOrder(pid) {
        var i = full.providerSlots.indexOf(pid);
        return i >= 0 ? i : 99;
    }
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    function dayKey(d) { return d.getFullYear() + "-" + full.pad(d.getMonth() + 1) + "-" + full.pad(d.getDate()); }

    // One bucket from a list of day keys.
    function bucketFor(label, tip, keys, muted) {
        var seg = {};
        var total = 0, cost = 0;
        for (var i = 0; i < keys.length; i++) {
            var day = full.usageHistory[keys[i]];
            if (!day)
                continue;
            for (var pid in day) {
                var id = full.seriesId(pid);
                if (!seg[id])
                    seg[id] = { id: id, tokens: 0, cost: 0 };
                seg[id].tokens += day[pid][0];
                seg[id].cost += day[pid][1];
                total += day[pid][0];
                cost += day[pid][1];
            }
        }
        var segments = Object.keys(seg).map(function (k) { return seg[k]; });
        segments.sort(function (a, b) { return full.seriesOrder(a.id) - full.seriesOrder(b.id); });
        return { label: label, tip: tip, segments: segments, total: total, cost: cost, muted: !!muted };
    }

    // Buckets for a period: days for a week or month (the whole calendar
    // month, future days empty — #395), months for a year.
    function periodBuckets(period, offset) {
        var now = new Date();
        var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        var out = [];
        if (period === "year") {
            var year = today.getFullYear() + offset;
            for (var m = 0; m < 12; m++) {
                var keys = [];
                var days = new Date(year, m + 1, 0).getDate();
                for (var d = 1; d <= days; d++)
                    keys.push(year + "-" + full.pad(m + 1) + "-" + full.pad(d));
                var first = new Date(year, m, 1);
                out.push(full.bucketFor(Qt.locale().monthName(m, Locale.NarrowFormat),
                                        Qt.locale().monthName(m) + " " + year, keys, first > today));
            }
            return out;
        }
        var start;
        var count;
        if (period === "week") {
            var monday = new Date(today);
            monday.setDate(today.getDate() - ((today.getDay() + 6) % 7) + offset * 7);
            start = monday;
            count = 7;
        } else {
            start = new Date(today.getFullYear(), today.getMonth() + offset, 1);
            count = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
        }
        for (var i = 0; i < count; i++) {
            var day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
            var label = period === "week" ? Qt.locale().dayName(day.getDay(), Locale.NarrowFormat)
                                          : String(day.getDate());
            out.push(full.bucketFor(label, Qt.locale().toString(day, "ddd d MMM"),
                                    [full.dayKey(day)], day > today));
        }
        return out;
    }

    function periodTitle(period, offset) {
        var now = new Date();
        if (period === "year")
            return String(now.getFullYear() + offset);
        if (period === "month") {
            var m = new Date(now.getFullYear(), now.getMonth() + offset, 1);
            return Qt.locale().monthName(m.getMonth()) + " " + m.getFullYear();
        }
        var monday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7) + offset * 7);
        var sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
        return Qt.locale().toString(monday, "d MMM") + " – "
               + Qt.locale().toString(sunday, "d MMM yyyy");
    }

    // Totals, active days, busiest bucket and per-provider shares.
    function summarize(buckets) {
        var total = 0, cost = 0, active = 0, elapsed = 0, best = null;
        var by = {};
        for (var i = 0; i < buckets.length; i++) {
            var b = buckets[i];
            total += b.total;
            cost += b.cost;
            if (b.total > 0)
                active++;
            if (!b.muted)
                elapsed++;
            if (!best || b.total > best.total)
                best = b;
            for (var j = 0; j < b.segments.length; j++) {
                var s = b.segments[j];
                if (!by[s.id])
                    by[s.id] = { id: s.id, tokens: 0, cost: 0 };
                by[s.id].tokens += s.tokens;
                by[s.id].cost += s.cost;
            }
        }
        var providers = Object.keys(by).map(function (k) { return by[k]; });
        providers.sort(function (a, b) { return full.seriesOrder(a.id) - full.seriesOrder(b.id); });
        return { total: total, cost: cost, active: active, elapsed: elapsed,
                 best: best && best.total > 0 ? best : null, providers: providers };
    }

    readonly property var recapBuckets: full.periodBuckets(full.recapPeriod, full.recapOffset)
    readonly property var recapSummary: full.summarize(full.recapBuckets)
    readonly property var monthTrend: full.periodBuckets("month", 0)
    // Earliest recorded day bounds how far back the recap can go.
    readonly property string firstDay: {
        var keys = Object.keys(full.usageHistory);
        return keys.length ? keys.sort()[0] : "";
    }
    // Last day (yyyy-mm-dd) of the period `offset` steps from now.
    function periodEndKey(period, offset) {
        var now = new Date();
        var end;
        if (period === "year")
            end = new Date(now.getFullYear() + offset, 11, 31);
        else if (period === "month")
            end = new Date(now.getFullYear(), now.getMonth() + offset + 1, 0);
        else {
            end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            end.setDate(end.getDate() - ((end.getDay() + 6) % 7) + offset * 7 + 6);
        }
        return full.dayKey(end);
    }
    // Back stops at the first period that still holds recorded history.
    readonly property bool recapCanGoBack: full.firstDay !== ""
        && full.periodEndKey(full.recapPeriod, full.recapOffset - 1) >= full.firstDay

    // ---- search, sort and filters (upstream #329) ----
    property string dexSearch: ""
    property bool shinyOnly: false
    property int dexSort: 0        // see dexSortModes
    property int logSort: 0        // see logSortModes
    readonly property var dexSortModes: [i18n("No. ↑"), i18n("No. ↓"), i18n("Name A–Z"),
                                         i18n("Name Z–A"), i18n("Rarity")]
    readonly property var logSortModes: [i18n("Newest"), i18n("Oldest"), i18n("No. ↑"),
                                         i18n("Name A–Z"), i18n("Rarity")]
    readonly property bool filtering: full.dexSearch !== "" || full.shinyOnly || full.rarityFilter !== ""

    // Case- and accent-insensitive, shared by both lists, so "pokemon"
    // finds "Pokémon"; "#25" or "25" matches by number.
    function fold(text) {
        return String(text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    }
    function matchesSearch(name, number) {
        var q = full.fold(full.dexSearch).trim();
        if (q === "")
            return true;
        var digits = q.replace(/^#/, "");
        if (/^\d+$/.test(digits))
            return String(number) === String(parseInt(digits, 10));
        return full.fold(name).indexOf(q) !== -1;
    }
    function rarityRank(r) {
        return { legendary: 0, rare: 1, uncommon: 2, common: 3 }[r] !== undefined
               ? { legendary: 0, rare: 1, uncommon: 2, common: 3 }[r] : 4;
    }
    function resetFilters() {
        full.dexSearch = "";
        full.shinyOnly = false;
        full.rarityFilter = "";
        full.dexPage = 0;
    }

    function filteredDex() {
        var out = full.dexItems.filter(function (d) {
            return (!full.rarityFilter || d.rarity === full.rarityFilter)
                && (!full.shinyOnly || d.is_shiny)
                && full.matchesSearch(d.name, d.species_id);
        });
        var byName = function (a, b) { return full.fold(a.name).localeCompare(full.fold(b.name)); };
        var sorters = [
            function (a, b) { return a.species_id - b.species_id; },
            function (a, b) { return b.species_id - a.species_id; },
            byName,
            function (a, b) { return byName(b, a); },
            function (a, b) { return full.rarityRank(a.rarity) - full.rarityRank(b.rarity)
                                     || a.species_id - b.species_id; }
        ];
        return out.sort(sorters[full.dexSort] || sorters[0]);
    }

    function filteredLog() {
        var last = function (e) { return e.chain.length ? e.chain[e.chain.length - 1] : {}; };
        var out = full.catchLog.filter(function (e) {
            return (!full.rarityFilter || e.rarity === full.rarityFilter)
                && (!full.shinyOnly || e.is_shiny)
                && e.chain.some(function (c) { return full.matchesSearch(c.name, c.species_id); });
        });
        var sorters = [
            function (a, b) { return (b.caught_at || 0) - (a.caught_at || 0); },
            function (a, b) { return (a.caught_at || 0) - (b.caught_at || 0); },
            function (a, b) { return (last(a).species_id || 0) - (last(b).species_id || 0); },
            function (a, b) { return full.fold(last(a).name).localeCompare(full.fold(last(b).name)); },
            function (a, b) { return full.rarityRank(a.rarity) - full.rarityRank(b.rarity); }
        ];
        // The companion being raised stays on top, as in the macOS app.
        var raising = out.filter(function (e) { return e.raising; });
        var rest = out.filter(function (e) { return !e.raising; });
        return raising.concat(rest.sort(sorters[full.logSort] || sorters[0]));
    }

    function pagedDex() {
        var all = filteredDex();
        var start = full.dexPage * full.dexPageSize;
        return all.slice(start, start + full.dexPageSize);
    }

    function dexPageCount() {
        return Math.max(1, Math.ceil(filteredDex().length / full.dexPageSize));
    }

    // Every action goes through poketokenctl, so validation and defaults stay
    // in the daemon rather than being duplicated here.
    Plasma5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []
        onNewData: function(sourceName) { disconnectSource(sourceName); }
        function run(cmd) { connectSource(cmd); }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        QQC2.TabBar {
            id: tabs
            Layout.fillWidth: true
            QQC2.TabButton { text: i18n("Home") }
            QQC2.TabButton { text: i18n("Shop") }
            QQC2.TabButton { text: i18n("Bag") }
            QQC2.TabButton { text: i18n("Collection") }
            QQC2.TabButton { text: i18n("Usage") }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: tabs.currentIndex

            // ======================= HOME =======================
            QQC2.ScrollView {
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    // Bind to the ScrollView's own availableWidth, not a fixed
                    // full.width - margin: a fixed width ignores the vertical
                    // scrollbar's reserved space once content overflows, so the
                    // scrollbar silently overlaps (and clips) right-aligned
                    // content instead of the layout narrowing to make room.
                    width: parent.availableWidth
                    spacing: Kirigami.Units.smallSpacing

                    // --- celebration banner ---
                    Rectangle {
                        Layout.fillWidth: true
                        visible: full.celebration !== null
                        implicitHeight: celebrationCol.implicitHeight + Kirigami.Units.largeSpacing
                        radius: Kirigami.Units.smallSpacing
                        color: full.celebration && full.celebration.kind === "shiny"
                               ? full.ctpYellow
                               : full.ctpMauve

                        ColumnLayout {
                            id: celebrationCol
                            anchors.centerIn: parent
                            width: parent.width - Kirigami.Units.largeSpacing
                            spacing: 0

                            PlasmaComponents.Label {
                                text: full.celebration
                                      ? (full.celebration.kind === "shiny" ? "✨ " : "")
                                        + full.celebration.title
                                      : ""
                                color: full.ctpBase
                                font.bold: true
                            }
                            PlasmaComponents.Label {
                                text: full.celebration ? full.celebration.detail : ""
                                color: full.ctpBase
                                opacity: 0.9
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // --- companion ---
                    RowLayout {
                        Layout.fillWidth: true
                        visible: full.companion !== null
                        spacing: Kirigami.Units.largeSpacing

                        AnimatedImage {
                            source: full.companion && full.companion.sprite_path
                                    ? "file://" + full.companion.sprite_path : ""
                            visible: full.companion !== null && full.companion.stage === "mon"
                            playing: visible
                            smooth: false
                            fillMode: Image.PreserveAspectFit
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                            Layout.preferredHeight: Kirigami.Units.gridUnit * 5
                        }

                        PlasmaComponents.Label {
                            text: "\u{1F95A}"
                            visible: full.companion !== null && full.companion.stage === "egg"
                            font.pixelSize: Kirigami.Units.gridUnit * 3
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: Kirigami.Units.smallSpacing

                                PlasmaExtras.Heading {
                                    level: 3
                                    text: {
                                        if (!full.companion)
                                            return "";
                                        if (full.companion.stage === "egg")
                                            return i18n("Egg");
                                        return full.companion.name
                                               ? full.companion.name
                                               : "#" + full.companion.species_id;
                                    }
                                }

                                Rectangle {
                                    // rarity/is_shiny/evo_line only exist once a companion has
                                    // hatched — an egg's payload omits them entirely.
                                    visible: full.companion !== null && full.companion.stage === "mon"
                                    radius: height / 2
                                    color: full.companion ? full.rarityColor(full.companion.rarity) : "grey"
                                    implicitWidth: rarityLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                    implicitHeight: rarityLabel.implicitHeight + 2

                                    PlasmaComponents.Label {
                                        id: rarityLabel
                                        anchors.centerIn: parent
                                        text: (full.companion && full.companion.stage === "mon")
                                              ? full.companion.rarity.toUpperCase() : ""
                                        color: full.ctpBase
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        font.bold: true
                                    }
                                }

                                PlasmaComponents.Label {
                                    text: "✨"
                                    visible: full.companion !== null && full.companion.stage === "mon"
                                             && full.companion.is_shiny
                                }

                                Repeater {
                                    model: full.companion && full.companion.profile
                                           ? full.companion.profile.types : []
                                    Rectangle {
                                        radius: height / 2
                                        color: full.typeColors[modelData] || full.ctpOverlay0
                                        implicitWidth: homeType.implicitWidth + Kirigami.Units.smallSpacing * 2
                                        implicitHeight: homeType.implicitHeight + 2
                                        PlasmaComponents.Label {
                                            id: homeType
                                            anchors.centerIn: parent
                                            text: modelData.toUpperCase()
                                            color: "white"
                                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                                            font.bold: true
                                        }
                                    }
                                }

                                // Repeat hatch of a graduated line grows 2× (#254).
                                PlasmaComponents.Label {
                                    text: i18n("2× growth")
                                    visible: full.companion !== null && full.companion.stage === "mon"
                                             && full.companion.growth_boost === true
                                    color: full.ctpGreen
                                    font.bold: true
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                }
                            }

                            PlasmaComponents.Label {
                                text: {
                                    if (!full.companion)
                                        return "";
                                    var parts = [];
                                    var p = full.companion.profile;
                                    if (p)
                                        parts.push(i18n("Lv. %1", p.level)
                                                   + (p.gender ? " " + full.genderSymbol(p.gender) : ""));
                                    if (full.companion.is_final_form)
                                        parts.push(i18n("Final form"));
                                    else if (full.companion.nature)
                                        parts.push(full.companion.nature);
                                    return parts.join(" · ");
                                }
                                opacity: 0.8
                            }

                            CatppuccinProgressBar {
                                Layout.fillWidth: true
                                from: 0
                                to: 1
                                value: full.companion
                                       ? (full.companion.stage === "egg"
                                          ? full.companion.egg_progress
                                          : full.companion.stage_progress)
                                       : 0
                                fillColor: full.ctpSapphire
                                trackColor: full.ctpSurface0

                                // Exact numbers on hover (#392): "X / Y · Z%".
                                MouseArea {
                                    id: growthHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                QQC2.ToolTip.visible: growthHover.containsMouse && full.companion !== null
                                QQC2.ToolTip.delay: 300
                                QQC2.ToolTip.text: {
                                    var c = full.companion;
                                    if (!c)
                                        return "";
                                    var used = c.stage === "egg" ? c.egg_usage : c.used_at_stage;
                                    var total = c.stage === "egg" ? c.egg_usage + c.remaining_tokens
                                                                  : c.stage_threshold;
                                    var pct = total > 0 ? Math.min(100, Math.floor(used * 100 / total)) : 0;
                                    return full.grouped(used) + " / " + full.grouped(total) + " · " + pct + "%";
                                }
                            }

                            PlasmaComponents.Label {
                                opacity: 0.7
                                text: {
                                    if (!full.companion)
                                        return "";
                                    if (full.companion.stage === "egg")
                                        return i18n("%1% to hatch",
                                                    Math.round(full.companion.egg_progress * 100));
                                    return i18n("%1 to %2",
                                                full.companion.remaining_text,
                                                full.companion.goal);
                                }
                            }

                            PlasmaComponents.Label {
                                text: full.companion ? full.companion.status_message : ""
                                font.bold: true
                            }
                        }
                    }

                    // --- evolution line strip ---
                    RowLayout {
                        Layout.fillWidth: true
                        visible: full.companion !== null && full.companion.stage === "mon"
                                 && full.companion.evo_line.length > 1
                        spacing: Kirigami.Units.largeSpacing

                        Repeater {
                            model: (full.companion && full.companion.stage === "mon")
                                   ? full.companion.evo_line : []

                            ColumnLayout {
                                spacing: 0

                                Image {
                                    source: modelData.sprite_path
                                            ? "file://" + modelData.sprite_path : ""
                                    smooth: false
                                    fillMode: Image.PreserveAspectFit
                                    // Unreached stages are dimmed rather than
                                    // hidden, so the whole line is visible.
                                    opacity: modelData.reached ? 1.0 : 0.35
                                    Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                                    Layout.preferredHeight: Kirigami.Units.gridUnit * 2
                                }

                                Rectangle {
                                    Layout.alignment: Qt.AlignHCenter
                                    width: Kirigami.Units.smallSpacing
                                    height: width
                                    radius: width / 2
                                    visible: modelData.current
                                    color: full.ctpMauve
                                }
                            }
                        }

                        Item { Layout.fillWidth: true }
                    }

                    Kirigami.Separator { Layout.fillWidth: true }

                    // --- today ---
                    PlasmaComponents.Label { text: i18n("Today's tokens"); opacity: 0.7 }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        PlasmaExtras.Heading {
                            level: 1
                            text: full.today ? full.today.tokens_compact : "—"
                        }

                        PlasmaComponents.Label {
                            text: full.today ? full.today.tokens_grouped : ""
                            opacity: 0.6
                        }

                        Item { Layout.fillWidth: true }

                        PlasmaComponents.Label {
                            text: full.today ? full.today.cost_text : ""
                            opacity: 0.9
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        visible: !!(full.periods && full.periods.week)
                        spacing: Kirigami.Units.smallSpacing

                        PlasmaComponents.Label { text: i18n("This week"); opacity: 0.6 }
                        PlasmaComponents.Label {
                            font.bold: true
                            text: full.periods && full.periods.week
                                  ? full.compact(full.periods.week.tokens) : ""
                        }
                        PlasmaComponents.Label {
                            opacity: 0.6
                            text: full.periods && full.periods.week
                                  ? full.money(full.periods.week.cost) : ""
                        }

                        Item { Layout.preferredWidth: Kirigami.Units.largeSpacing }

                        PlasmaComponents.Label { text: i18n("This month"); opacity: 0.6 }
                        PlasmaComponents.Label {
                            font.bold: true
                            text: full.periods && full.periods.month
                                  ? full.compact(full.periods.month.tokens) : ""
                        }
                        PlasmaComponents.Label {
                            opacity: 0.6
                            text: full.periods && full.periods.month
                                  ? full.money(full.periods.month.cost) : ""
                        }

                        Item { Layout.fillWidth: true }
                    }

                    // --- this month's daily trend, stacked by provider (#270/#348/#395) ---
                    UsageChart {
                        Layout.fillWidth: true
                        Layout.preferredHeight: Kirigami.Units.gridUnit * 4
                        visible: Object.keys(full.usageHistory).length > 0
                        buckets: full.monthTrend
                        labelEvery: 5
                        colorFor: full.providerColor
                        nameFor: full.providerName
                        formatTokens: full.compact
                        formatCost: full.money
                        surface: full.ctpBase
                        axisColor: full.ctpSurface1
                    }

                    QQC2.Button {
                        Layout.alignment: Qt.AlignRight
                        visible: Object.keys(full.usageHistory).length > 0
                        flat: true
                        text: i18n("Usage recap ›")
                        onClicked: tabs.currentIndex = 4
                    }

                    // --- per-provider breakdown ---
                    Repeater {
                        model: full.providers ? Object.keys(full.providers) : []

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            RowLayout {
                                Layout.fillWidth: true
                                PlasmaComponents.Label { text: modelData; font.bold: true }
                                Item { Layout.fillWidth: true }
                                PlasmaComponents.Label {
                                    text: full.providers[modelData].total_tokens_compact
                                    font.bold: true
                                }
                                PlasmaComponents.Label {
                                    text: full.money(full.providers[modelData].total_cost)
                                    opacity: 0.7
                                }
                            }

                            PlasmaComponents.Label {
                                opacity: 0.6
                                text: i18n("in %1 · out %2 · cache w %3 · cache r %4",
                                           full.compact(full.providers[modelData].input_tokens),
                                           full.compact(full.providers[modelData].output_tokens),
                                           full.compact(full.providers[modelData].cache_creation_tokens),
                                           full.compact(full.providers[modelData].cache_read_tokens))
                            }
                        }
                    }

                    Kirigami.Separator { Layout.fillWidth: true; visible: full.hasLimits }

                    // --- limits ---
                    PlasmaExtras.Heading {
                        level: 4
                        visible: full.hasLimits
                        text: full.limits && full.limits.plan
                              ? i18n("Limits (official) · %1", full.limits.plan.toUpperCase())
                              : i18n("Limits (official)")
                    }

                    // Token totals sum every account used on this machine;
                    // limits belong to whichever is logged in. Naming it keeps
                    // that difference visible.
                    PlasmaComponents.Label {
                        visible: text.length > 0
                        opacity: 0.7
                        text: {
                            if (!full.limits || !full.limits.account)
                                return "";
                            var a = full.limits.account;
                            var who = a.email ? a.email : a.display_name;
                            if (!who)
                                return "";
                            return a.organization
                                   ? i18n("for %1 · %2", who, a.organization)
                                   : i18n("for %1", who);
                        }
                    }

                    Repeater {
                        model: {
                            if (!full.limits)
                                return [];
                            var out = [];
                            if (full.limits.session)
                                out.push({ "label": i18n("5-hour session"),
                                           "kind": "session", "w": full.limits.session });
                            if (full.limits.weekly)
                                out.push({ "label": i18n("Weekly"),
                                           "kind": "weekly", "w": full.limits.weekly });
                            return out;
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                Layout.fillWidth: true
                                PlasmaComponents.Label { text: modelData.label }
                                Item { Layout.fillWidth: true }
                                PlasmaComponents.Label {
                                    text: Math.round(modelData.w.utilization) + "%"
                                    color: full.levelColor(modelData.w.utilization)
                                    font.bold: true
                                }
                            }

                            CatppuccinProgressBar {
                                Layout.fillWidth: true
                                from: 0
                                to: 100
                                value: modelData.w.utilization
                                fillColor: full.levelColor(modelData.w.utilization)
                                trackColor: full.ctpSurface0
                            }

                            RowLayout {
                                Layout.fillWidth: true

                                PlasmaComponents.Label {
                                    text: full.resetIn(modelData.w.resets_at)
                                    opacity: 0.7
                                }

                                Item { Layout.fillWidth: true }

                                PlasmaComponents.Label {
                                    visible: text.length > 0
                                    opacity: 0.9
                                    color: full.ctpPeach
                                    text: {
                                        var b = full.burn[modelData.kind];
                                        if (!b || !b.eta_text)
                                            return "";
                                        return i18n("at this rate, full at %1", b.eta_text);
                                    }
                                }
                            }
                        }
                    }

                    // --- provider incidents ---
                    Repeater {
                        model: Object.keys(full.providerStatus)

                        RowLayout {
                            Layout.fillWidth: true

                            PlasmaComponents.Label {
                                text: modelData
                                opacity: 0.8
                            }

                            Item { Layout.fillWidth: true }

                            PlasmaComponents.Label {
                                text: full.providerStatus[modelData].label
                                color: full.providerStatus[modelData].severity === "crit"
                                       ? full.ctpRed
                                       : full.ctpPeach
                                font.bold: true
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ======================= SHOP =======================
            QQC2.ScrollView {
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    // Bind to the ScrollView's own availableWidth, not a fixed
                    // full.width - margin: a fixed width ignores the vertical
                    // scrollbar's reserved space once content overflows, so the
                    // scrollbar silently overlaps (and clips) right-aligned
                    // content instead of the layout narrowing to make room.
                    width: parent.availableWidth
                    spacing: Kirigami.Units.smallSpacing

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        PlasmaComponents.Label { text: i18n("Spendable tokens"); opacity: 0.7 }
                        PlasmaExtras.Heading {
                            level: 1
                            text: full.companion ? full.companion.spendable_text : "0"
                        }
                        PlasmaComponents.Label {
                            text: i18n("Spend the tokens you've used on items.")
                            opacity: 0.6
                        }
                    }

                    Repeater {
                        model: full.shopItems

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Image {
                                source: modelData.sprite_path
                                        ? "file://" + modelData.sprite_path : ""
                                visible: modelData.sprite_path !== ""
                                smooth: false
                                fillMode: Image.PreserveAspectFit
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
                                Layout.preferredHeight: Kirigami.Units.gridUnit * 1.5
                            }

                            PlasmaComponents.Label {
                                text: modelData.emoji
                                visible: modelData.sprite_path === ""
                                font.pixelSize: Kirigami.Units.gridUnit
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                RowLayout {
                                    spacing: Kirigami.Units.smallSpacing

                                    PlasmaComponents.Label {
                                        text: modelData.label
                                        font.bold: true
                                    }

                                    PlasmaComponents.Label {
                                        text: i18n("Owned ×%1", modelData.owned_count)
                                        visible: modelData.owned_count > 0
                                        opacity: 0.6
                                    }

                                    Rectangle {
                                        visible: modelData.badge !== ""
                                        radius: height / 2
                                        color: full.rarityColor(modelData.badge.toLowerCase())
                                        implicitWidth: badgeLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                        implicitHeight: badgeLabel.implicitHeight + 2

                                        PlasmaComponents.Label {
                                            id: badgeLabel
                                            anchors.centerIn: parent
                                            text: modelData.badge
                                            color: full.ctpBase
                                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                                            font.bold: true
                                        }
                                    }
                                }

                                PlasmaComponents.Label {
                                    text: modelData.description
                                    opacity: 0.7
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }

                                PlasmaComponents.Label {
                                    // Eggs stay listed while incubating, just not buyable (#261).
                                    text: i18n("Available once your current egg hatches.")
                                    visible: modelData.locked === true
                                    color: full.ctpPeach
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }

                                PlasmaComponents.Label {
                                    // A released Pokémon keeps its Pokédex entry (#242, #291).
                                    text: i18n("Your current Pokémon is released but stays in your Pokédex.")
                                    visible: modelData.kind === "egg" && full.companion !== null
                                             && full.companion.stage === "mon"
                                    opacity: 0.6
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }

                                PlasmaComponents.Label {
                                    readonly property int qty: full.quantity(modelData.key, modelData.max_count)
                                    text: qty > 1
                                          ? i18n("Total %1 (×%2)", full.compact(modelData.price * qty), qty)
                                          : i18n("Price %1", modelData.price_text)
                                    opacity: 0.6
                                }

                                PlasmaComponents.Label {
                                    text: full.confirmStage === 2
                                          ? i18n("This is a legendary or shiny Pokémon. Release it anyway?")
                                          : i18n("Release your current Pokémon for this egg?")
                                    visible: full.confirmKey === modelData.key
                                    color: full.confirmStage === 2 ? full.ctpRed : full.ctpPeach
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }
                            }

                            // Consumables can be bought several at once (#371).
                            QQC2.SpinBox {
                                visible: modelData.stackable && modelData.max_count > 1
                                from: 1
                                to: Math.max(1, modelData.max_count)
                                value: full.quantity(modelData.key, modelData.max_count)
                                editable: true
                                onValueModified: full.setQuantity(modelData.key, value)
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                            }

                            QQC2.Button {
                                readonly property bool confirming: full.confirmKey === modelData.key
                                text: {
                                    if (modelData.owned)
                                        return i18n("Owned");
                                    if (confirming)
                                        return full.confirmStage === 2 ? i18n("Release") : i18n("Confirm");
                                    return i18n("Buy");
                                }
                                enabled: modelData.affordable
                                onClicked: {
                                    if (modelData.kind === "egg") {
                                        full.buyEgg(modelData.key);
                                        return;
                                    }
                                    var qty = modelData.stackable
                                              ? full.quantity(modelData.key, modelData.max_count) : 1;
                                    runner.run("poketokenctl buy " + modelData.key + " " + qty);
                                    full.setQuantity(modelData.key, 1);
                                }
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ======================= BAG =======================
            ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    visible: full.bagItems.length === 0
                    PlasmaComponents.Label {
                        text: i18n("Your bag is empty.")
                        opacity: 0.7
                    }
                    // A way out of the dead end (#366).
                    QQC2.Button {
                        text: i18n("Visit the shop")
                        onClicked: tabs.currentIndex = 1
                    }
                }

                Repeater {
                    model: full.bagItems

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        Image {
                            source: modelData.sprite_path
                                    ? "file://" + modelData.sprite_path : ""
                            visible: modelData.sprite_path !== ""
                            smooth: false
                            fillMode: Image.PreserveAspectFit
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
                            Layout.preferredHeight: Kirigami.Units.gridUnit * 1.5
                        }

                        PlasmaComponents.Label {
                            text: modelData.emoji
                            visible: modelData.sprite_path === ""
                            font.pixelSize: Kirigami.Units.gridUnit
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            PlasmaComponents.Label {
                                text: modelData.label + " ×" + modelData.count
                                font.bold: true
                            }
                            PlasmaComponents.Label {
                                text: modelData.description
                                opacity: 0.7
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            PlasmaComponents.Label {
                                text: modelData.effect
                                opacity: 0.6
                            }

                            // Growth preview for the chosen number of candies (#328).
                            PlasmaComponents.Label {
                                readonly property var preview: {
                                    if (!modelData.previews || modelData.previews.length === 0)
                                        return null;
                                    var n = full.quantity("bag:" + modelData.key, modelData.previews.length);
                                    return modelData.previews[n - 1];
                                }
                                visible: preview !== null
                                text: {
                                    if (!preview)
                                        return "";
                                    if (preview.graduates)
                                        return preview.used < preview.count
                                               ? i18n("Graduates after %1 — the rest stay in your bag", preview.used)
                                               : i18n("Graduates!");
                                    if (preview.evolutions > 0)
                                        return i18np("Evolves once", "Evolves %1 times", preview.evolutions);
                                    return i18n("Stage progress → %1%", Math.round(preview.stage_progress * 100));
                                }
                                color: full.ctpGreen
                            }
                        }

                        QQC2.SpinBox {
                            visible: modelData.previews !== undefined && modelData.previews.length > 1
                            from: 1
                            to: modelData.previews ? Math.max(1, modelData.previews.length) : 1
                            value: full.quantity("bag:" + modelData.key,
                                                 modelData.previews ? modelData.previews.length : 1)
                            editable: true
                            onValueModified: full.setQuantity("bag:" + modelData.key, value)
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                        }

                        QQC2.Button {
                            text: modelData.passive ? i18n("Active") : i18n("Use")
                            enabled: modelData.usable
                            onClicked: {
                                var qty = modelData.previews
                                          ? full.quantity("bag:" + modelData.key, modelData.previews.length) : 1;
                                runner.run("poketokenctl use " + modelData.key + " " + qty);
                                full.setQuantity("bag:" + modelData.key, 1);
                            }
                        }
                    }
                }

                Item { Layout.fillHeight: true }
            }

            // ==================== COLLECTION ====================
            ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                QQC2.TabBar {
                    id: collectionTabs
                    Layout.fillWidth: true
                    QQC2.TabButton { text: i18n("Pokédex") }
                    QQC2.TabButton { text: i18n("Catch log") }
                }

                // search, sort and shiny-only (upstream #329), shared by both sub-tabs
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.SearchField {
                        Layout.fillWidth: true
                        placeholderText: i18n("Search name or #number")
                        text: full.dexSearch
                        onTextChanged: {
                            full.dexSearch = text;
                            full.dexPage = 0;
                        }
                    }

                    QQC2.ComboBox {
                        model: collectionTabs.currentIndex === 1 ? full.logSortModes : full.dexSortModes
                        currentIndex: collectionTabs.currentIndex === 1 ? full.logSort : full.dexSort
                        onActivated: function (index) {
                            if (collectionTabs.currentIndex === 1)
                                full.logSort = index;
                            else
                                full.dexSort = index;
                            full.dexPage = 0;
                        }
                    }

                    QQC2.Button {
                        checkable: true
                        checked: full.shinyOnly
                        text: "✨"
                        QQC2.ToolTip.text: i18n("Shiny only")
                        QQC2.ToolTip.visible: hovered
                        onClicked: {
                            full.shinyOnly = !full.shinyOnly;
                            full.dexPage = 0;
                        }
                    }
                }

                // rarity filters, shared by both sub-tabs
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Repeater {
                        model: ["legendary", "rare", "uncommon", "common"]

                        QQC2.Button {
                            checkable: true
                            checked: full.rarityFilter === modelData
                            text: modelData.charAt(0).toUpperCase() + modelData.slice(1)
                                  + " " + (full.activeCounts[modelData] !== undefined
                                           ? full.activeCounts[modelData] : 0)
                            onClicked: {
                                // Clicking the active filter clears it.
                                full.rarityFilter = full.rarityFilter === modelData ? "" : modelData;
                                full.dexPage = 0;
                            }
                        }
                    }

                    Item { Layout.fillWidth: true }
                }

                // A plain Item rather than a StackLayout: StackLayout kept the
                // Pokédex page at a stale narrow width whenever its implicit
                // size changed (e.g. opening an entry), so the pages fill this
                // Item and toggle visibility instead.
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    // ---- Pokédex grid ----
                    ColumnLayout {
                        anchors.fill: parent
                        visible: collectionTabs.currentIndex === 0
                        spacing: Kirigami.Units.smallSpacing

                        PlasmaComponents.Label {
                            text: i18n("%1 species", full.dexItems.length)
                            font.bold: true
                        }

                        // ---- entry detail (opened by clicking a sprite, #394) ----
                        Rectangle {
                            Layout.fillWidth: true
                            visible: full.selectedEntry !== null
                            radius: Kirigami.Units.smallSpacing
                            color: full.ctpSurface0
                            implicitWidth: detailRow.implicitWidth + Kirigami.Units.largeSpacing
                            implicitHeight: detailRow.implicitHeight + Kirigami.Units.largeSpacing

                            RowLayout {
                                id: detailRow
                                anchors.fill: parent
                                anchors.margins: Kirigami.Units.smallSpacing
                                spacing: Kirigami.Units.largeSpacing

                                Image {
                                    source: full.selectedEntry && full.selectedEntry.sprite_path
                                            ? "file://" + full.selectedEntry.sprite_path : ""
                                    smooth: false
                                    fillMode: Image.PreserveAspectFit
                                    Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                                    Layout.preferredHeight: Kirigami.Units.gridUnit * 4
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    PlasmaExtras.Heading {
                                        level: 4
                                        text: full.selectedEntry
                                              ? (full.selectedEntry.is_shiny ? "✨" : "")
                                                + "#" + full.selectedEntry.species_id + " "
                                                + (full.selectedEntry.name || "")
                                              : ""
                                    }
                                    PlasmaComponents.Label {
                                        text: full.selectedEntry
                                              ? full.selectedEntry.rarity.charAt(0).toUpperCase()
                                                + full.selectedEntry.rarity.slice(1)
                                                + (full.selectedEntry.is_raising ? " · " + i18n("raising") : "")
                                              : ""
                                        color: full.selectedEntry ? full.rarityColor(full.selectedEntry.rarity) : "grey"
                                    }

                                    // ---- individual values (#264) ----
                                    RowLayout {
                                        visible: full.selectedProfile !== null
                                        spacing: Kirigami.Units.smallSpacing

                                        PlasmaComponents.Label {
                                            text: full.selectedProfile
                                                  ? i18n("Lv. %1", full.selectedProfile.level) + " "
                                                    + full.genderSymbol(full.selectedProfile.gender)
                                                  : ""
                                            font.bold: true
                                        }

                                        Repeater {
                                            model: full.selectedProfile ? full.selectedProfile.types : []
                                            Rectangle {
                                                radius: height / 2
                                                color: full.typeColors[modelData] || full.ctpOverlay0
                                                implicitWidth: typeLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                                implicitHeight: typeLabel.implicitHeight + 2
                                                PlasmaComponents.Label {
                                                    id: typeLabel
                                                    anchors.centerIn: parent
                                                    text: modelData.toUpperCase()
                                                    color: "white"
                                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                    font.bold: true
                                                }
                                            }
                                        }
                                    }

                                    PlasmaComponents.Label {
                                        visible: full.selectedProfile !== null && full.selectedProfile.ability !== ""
                                        text: full.selectedProfile
                                              ? i18n("Ability: %1", full.selectedProfile.ability)
                                                + (full.selectedProfile.ability_hidden ? " " + i18n("(hidden)") : "")
                                              : ""
                                        opacity: 0.8
                                    }

                                    // Computed stats once details are cached; IVs alone before that.
                                    GridLayout {
                                        visible: full.selectedProfile !== null
                                        columns: 3
                                        columnSpacing: Kirigami.Units.smallSpacing
                                        rowSpacing: 0

                                        Repeater {
                                            model: {
                                                var p = full.selectedProfile;
                                                if (!p)
                                                    return [];
                                                if (p.stats.length > 0)
                                                    return p.stats;
                                                return p.ivs.map(function (iv) {
                                                    return { name: iv.name, iv: iv.value, value: -1, base: 0 };
                                                });
                                            }

                                            delegate: RowLayout {
                                                spacing: 2
                                                PlasmaComponents.Label {
                                                    text: full.statLabels[modelData.name] || modelData.name
                                                    opacity: 0.7
                                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                    Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
                                                }
                                                PlasmaComponents.Label {
                                                    text: (modelData.value >= 0 ? modelData.value + " " : "")
                                                          + i18n("IV %1", modelData.iv)
                                                    // A perfect IV is worth noticing.
                                                    color: modelData.iv === 31 ? full.ctpGreen : full.ctpText
                                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                }
                                            }
                                        }
                                    }

                                    // ---- Unown letters (#288): owned ones pin, the rest are dimmed ----
                                    GridLayout {
                                        visible: full.selectedEntry !== null
                                                 && full.selectedEntry.unown_forms !== undefined
                                        columns: 7
                                        columnSpacing: 2
                                        rowSpacing: 2

                                        Repeater {
                                            model: full.selectedEntry && full.selectedEntry.unown_forms
                                                   ? full.selectedEntry.unown_forms : []

                                            Rectangle {
                                                implicitWidth: Kirigami.Units.gridUnit * 1.6
                                                implicitHeight: Kirigami.Units.gridUnit * 1.6
                                                radius: 3
                                                color: modelData.is_representative ? full.ctpMauve : "transparent"
                                                border.color: full.ctpSurface1
                                                opacity: modelData.collected ? 1.0 : 0.3

                                                Image {
                                                    anchors.fill: parent
                                                    source: modelData.sprite_path
                                                            ? "file://" + modelData.sprite_path : ""
                                                    visible: modelData.sprite_path !== ""
                                                    smooth: false
                                                    fillMode: Image.PreserveAspectFit
                                                }
                                                PlasmaComponents.Label {
                                                    anchors.centerIn: parent
                                                    visible: modelData.sprite_path === ""
                                                    text: modelData.symbol
                                                }
                                                MouseArea {
                                                    anchors.fill: parent
                                                    enabled: modelData.collected
                                                    cursorShape: modelData.collected ? Qt.PointingHandCursor
                                                                                     : Qt.ArrowCursor
                                                    onClicked: runner.run("poketokenctl pin "
                                                        + full.selectedEntry.species_id + " " + modelData.form)
                                                }
                                            }
                                        }
                                    }

                                    PlasmaComponents.Label {
                                        visible: full.selectedProfile !== null && full.selectedProfile.moves.length > 0
                                        text: full.selectedProfile ? i18n("Moves: %1", full.selectedProfile.moves.join(", ")) : ""
                                        opacity: 0.8
                                        wrapMode: Text.Wrap
                                        Layout.fillWidth: true
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        // Pin as the panel / floating pet representative (#158).
                                        QQC2.Button {
                                            readonly property bool pinned: full.selectedEntry !== null
                                                                           && full.selectedEntry.is_representative === true
                                            text: pinned ? i18n("Unpin from panel") : i18n("Show in panel")
                                            onClicked: runner.run("poketokenctl pin "
                                                                  + (pinned ? "none" : full.selectedEntry.species_id))
                                        }
                                        // Both appearances owned: choose which one the panel shows (#345).
                                        QQC2.Button {
                                            // Unown's shine is per letter, chosen in the letter grid.
                                            visible: full.selectedEntry !== null && full.selectedEntry.has_normal
                                                     && full.selectedEntry.has_shiny
                                                     && full.selectedEntry.unown_forms === undefined
                                            readonly property bool showingShiny: full.selectedEntry !== null
                                                && full.selectedEntry.representative_shiny !== false
                                            text: showingShiny ? i18n("Show normal") : i18n("Show shiny ✨")
                                            onClicked: runner.run("poketokenctl pin " + full.selectedEntry.species_id
                                                                  + (showingShiny ? " --normal" : " --shiny"))
                                        }
                                        QQC2.Button {
                                            text: i18n("Close")
                                            onClicked: full.selectedSpecies = -1
                                        }
                                    }
                                }
                            }
                        }

                        PlasmaComponents.Label {
                            text: i18n("No Pokémon caught yet!")
                            visible: full.dexItems.length === 0
                            opacity: 0.7
                        }

                        RowLayout {
                            visible: full.dexItems.length > 0 && full.filteredDex().length === 0
                            PlasmaComponents.Label { text: i18n("No matches."); opacity: 0.7 }
                            QQC2.Button { text: i18n("Reset filters"); onClicked: full.resetFilters() }
                        }

                        GridLayout {
                            Layout.fillWidth: true
                            columns: 4

                            // The scroll wheel turns pages (#393).
                            WheelHandler {
                                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                onWheel: function (event) {
                                    if (event.angleDelta.y < 0 && full.dexPage < full.dexPageCount() - 1)
                                        full.dexPage = full.dexPage + 1;
                                    else if (event.angleDelta.y > 0 && full.dexPage > 0)
                                        full.dexPage = full.dexPage - 1;
                                }
                            }
                            columnSpacing: Kirigami.Units.smallSpacing
                            rowSpacing: Kirigami.Units.smallSpacing

                            Repeater {
                                model: full.pagedDex()

                                ColumnLayout {
                                    spacing: 0

                                    RowLayout {
                                        spacing: 2

                                        PlasmaComponents.Label {
                                            text: "#" + modelData.final_id
                                                  + (modelData.unown_count !== undefined
                                                     ? " · " + modelData.unown_count + "/" + modelData.unown_total : "")
                                            // Rarity reads at a glance in the unfiltered view (#343).
                                            color: full.rarityColor(modelData.rarity)
                                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        }

                                        // Backed only by the companion being
                                        // raised: buying an egg discards it and
                                        // this square disappears, so it is
                                        // marked rather than shown as permanent.
                                        Rectangle {
                                            visible: modelData.is_raising === true
                                            radius: height / 2
                                            color: full.ctpSapphire
                                            implicitWidth: raisingTag.implicitWidth + 6
                                            implicitHeight: raisingTag.implicitHeight + 2

                                            PlasmaComponents.Label {
                                                id: raisingTag
                                                anchors.centerIn: parent
                                                text: i18n("RAISING")
                                                color: full.ctpBase
                                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                font.bold: true
                                            }
                                        }
                                    }

                                    Image {
                                        source: modelData.sprite_path
                                                ? "file://" + modelData.sprite_path : ""
                                        smooth: false
                                        fillMode: Image.PreserveAspectFit
                                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.5
                                        Layout.preferredHeight: Kirigami.Units.gridUnit * 2.5

                                        // Lift on hover to show it is clickable (#343).
                                        scale: cellHover.containsMouse ? 1.08 : 1.0
                                        Behavior on scale { NumberAnimation { duration: 120 } }

                                        MouseArea {
                                            id: cellHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: full.selectedSpecies =
                                                full.selectedSpecies === modelData.species_id ? -1 : modelData.species_id
                                        }

                                        PlasmaComponents.Label {
                                            // The pinned representative.
                                            anchors.top: parent.top
                                            anchors.right: parent.right
                                            text: "📌"
                                            visible: modelData.is_representative === true
                                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                                        }
                                    }

                                    PlasmaComponents.Label {
                                        text: (modelData.is_shiny ? "✨" : "")
                                              + (modelData.name ? modelData.name
                                                                : "#" + modelData.final_id)
                                        elide: Text.ElideRight
                                        // Not permanent yet: buying an egg
                                        // discards the companion and this
                                        // square disappears.
                                        opacity: modelData.is_raising ? 0.65 : 1.0
                                        font.italic: modelData.is_raising === true
                                        Layout.maximumWidth: Kirigami.Units.gridUnit * 4
                                    }
                                }
                            }
                        }

                        Item { Layout.fillHeight: true }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: full.dexPageCount() > 1

                            QQC2.Button {
                                text: "‹"
                                enabled: full.dexPage > 0
                                onClicked: full.dexPage = full.dexPage - 1
                            }

                            Item { Layout.fillWidth: true }

                            PlasmaComponents.Label {
                                text: (full.dexPage + 1) + " / " + full.dexPageCount()
                            }

                            Item { Layout.fillWidth: true }

                            QQC2.Button {
                                text: "›"
                                enabled: full.dexPage < full.dexPageCount() - 1
                                onClicked: full.dexPage = full.dexPage + 1
                            }
                        }
                    }

                    // ---- Catch log ----
                    QQC2.ScrollView {
                        anchors.fill: parent
                        visible: collectionTabs.currentIndex === 1
                        clip: true
                        contentWidth: availableWidth

                        ColumnLayout {
                            // Bind to the ScrollView's own availableWidth, not a fixed
                    // full.width - margin: a fixed width ignores the vertical
                    // scrollbar's reserved space once content overflows, so the
                    // scrollbar silently overlaps (and clips) right-aligned
                    // content instead of the layout narrowing to make room.
                    width: parent.availableWidth
                            spacing: Kirigami.Units.smallSpacing

                            PlasmaComponents.Label {
                                text: i18n("%1 total", full.catchLog.length)
                                font.bold: true
                            }

                            Repeater {
                                model: full.filteredLog()

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Kirigami.Units.smallSpacing

                                        Rectangle {
                                            radius: height / 2
                                            color: full.rarityColor(modelData.rarity)
                                            implicitWidth: logRarity.implicitWidth + Kirigami.Units.smallSpacing * 2
                                            implicitHeight: logRarity.implicitHeight + 2

                                            PlasmaComponents.Label {
                                                id: logRarity
                                                anchors.centerIn: parent
                                                text: modelData.rarity.toUpperCase()
                                                color: full.ctpBase
                                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                font.bold: true
                                            }
                                        }

                                        Rectangle {
                                            visible: modelData.raising
                                            radius: height / 2
                                            color: full.ctpSapphire
                                            implicitWidth: raisingLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                            implicitHeight: raisingLabel.implicitHeight + 2

                                            PlasmaComponents.Label {
                                                id: raisingLabel
                                                anchors.centerIn: parent
                                                text: i18n("RAISING")
                                                color: full.ctpBase
                                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                font.bold: true
                                            }
                                        }

                                        // Released for an egg rather than graduated (#242).
                                        Rectangle {
                                            visible: modelData.released === true
                                            radius: height / 2
                                            color: full.ctpOverlay0
                                            implicitWidth: releasedLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                            implicitHeight: releasedLabel.implicitHeight + 2

                                            PlasmaComponents.Label {
                                                id: releasedLabel
                                                anchors.centerIn: parent
                                                text: i18n("RELEASED")
                                                color: full.ctpBase
                                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                font.bold: true
                                            }
                                        }

                                        Item { Layout.fillWidth: true }

                                        PlasmaComponents.Label {
                                            text: {
                                                var parts = [];
                                                if (modelData.level)
                                                    parts.push(i18n("Lv. %1", modelData.level)
                                                               + (modelData.gender ? " " + full.genderSymbol(modelData.gender) : ""));
                                                if (modelData.nature)
                                                    parts.push(modelData.nature);
                                                return parts.join(" · ");
                                            }
                                            opacity: 0.7
                                        }
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        Repeater {
                                            model: modelData.chain

                                            RowLayout {
                                                spacing: 2

                                                PlasmaComponents.Label {
                                                    text: "→"
                                                    visible: index > 0
                                                    opacity: 0.5
                                                }

                                                ColumnLayout {
                                                    spacing: 0
                                                    Image {
                                                        source: modelData.sprite_path
                                                                ? "file://" + modelData.sprite_path : ""
                                                        smooth: false
                                                        fillMode: Image.PreserveAspectFit
                                                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                                                        Layout.preferredHeight: Kirigami.Units.gridUnit * 2
                                                    }
                                                    PlasmaComponents.Label {
                                                        text: modelData.name
                                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                                    }
                                                }
                                            }
                                        }

                                        Item { Layout.fillWidth: true }
                                    }

                                    PlasmaComponents.Label {
                                        text: modelData.raised_text
                                        visible: text.length > 0
                                        opacity: 0.6
                                    }

                                    Kirigami.Separator { Layout.fillWidth: true }
                                }
                            }

                            Item { Layout.fillHeight: true }
                        }
                    }
                }
            }

            // ======================= USAGE (recap) =======================
            QQC2.ScrollView {
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    // Bind to the ScrollView's own availableWidth, not a fixed
                    // full.width - margin: a fixed width ignores the vertical
                    // scrollbar's reserved space once content overflows, so the
                    // scrollbar silently overlaps (and clips) right-aligned
                    // content instead of the layout narrowing to make room.
                    width: parent.availableWidth
                    spacing: Kirigami.Units.smallSpacing

                    // Period controls in one row above the chart.
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        Repeater {
                            model: [{ key: "week", label: i18n("Week") },
                                    { key: "month", label: i18n("Month") },
                                    { key: "year", label: i18n("Year") }]
                            QQC2.Button {
                                checkable: true
                                checked: full.recapPeriod === modelData.key
                                text: modelData.label
                                onClicked: {
                                    full.recapPeriod = modelData.key;
                                    full.recapOffset = 0;
                                }
                            }
                        }

                        Item { Layout.fillWidth: true }

                        QQC2.Button {
                            text: "‹"
                            enabled: full.recapCanGoBack
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                            onClicked: full.recapOffset = full.recapOffset - 1
                        }
                        QQC2.Button {
                            text: "›"
                            enabled: full.recapOffset < 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                            onClicked: full.recapOffset = full.recapOffset + 1
                        }
                        // Back to the current period in one click (#367).
                        QQC2.Button {
                            visible: full.recapOffset !== 0
                            text: i18n("Now")
                            onClicked: full.recapOffset = 0
                        }
                    }

                    PlasmaExtras.Heading {
                        level: 4
                        text: full.periodTitle(full.recapPeriod, full.recapOffset)
                    }

                    RowLayout {
                        spacing: Kirigami.Units.largeSpacing
                        ColumnLayout {
                            spacing: 0
                            PlasmaComponents.Label { text: i18n("Tokens"); opacity: 0.6 }
                            PlasmaExtras.Heading { level: 2; text: full.compact(full.recapSummary.total) }
                        }
                        ColumnLayout {
                            spacing: 0
                            PlasmaComponents.Label { text: i18n("Cost"); opacity: 0.6 }
                            PlasmaExtras.Heading { level: 2; text: full.money(full.recapSummary.cost) }
                        }
                        ColumnLayout {
                            spacing: 0
                            PlasmaComponents.Label {
                                text: full.recapPeriod === "year" ? i18n("Active months") : i18n("Active days")
                                opacity: 0.6
                            }
                            PlasmaExtras.Heading {
                                level: 2
                                // Out of the days (or months) so far, not the whole period.
                                text: full.recapSummary.active + " / " + full.recapSummary.elapsed
                            }
                        }
                    }

                    PlasmaComponents.Label {
                        visible: full.recapSummary.best !== null
                        text: full.recapSummary.best
                              ? i18n("Busiest: %1 — %2", full.recapSummary.best.tip,
                                     full.compact(full.recapSummary.best.total))
                              : ""
                        opacity: 0.8
                    }

                    UsageChart {
                        Layout.fillWidth: true
                        Layout.preferredHeight: Kirigami.Units.gridUnit * 8
                        buckets: full.recapBuckets
                        labelEvery: full.recapPeriod === "month" ? 5 : 1
                        colorFor: full.providerColor
                        nameFor: full.providerName
                        formatTokens: full.compact
                        formatCost: full.money
                        surface: full.ctpBase
                        axisColor: full.ctpSurface1
                    }

                    PlasmaComponents.Label {
                        visible: full.recapSummary.total === 0
                        text: i18n("No usage recorded in this period.")
                        opacity: 0.7
                    }

                    // Legend + per-provider totals: identity is never colour alone.
                    Repeater {
                        model: full.recapSummary.providers

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Rectangle {
                                width: Kirigami.Units.smallSpacing * 2
                                height: width
                                radius: 2
                                color: full.providerColor(modelData.id)
                            }
                            PlasmaComponents.Label { text: full.providerName(modelData.id) }
                            Item { Layout.fillWidth: true }
                            PlasmaComponents.Label {
                                text: full.compact(modelData.tokens)
                                font.bold: true
                            }
                            PlasmaComponents.Label {
                                text: full.recapSummary.total > 0
                                      ? Math.round(modelData.tokens * 100 / full.recapSummary.total) + "%" : ""
                                opacity: 0.6
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                                horizontalAlignment: Text.AlignRight
                            }
                            PlasmaComponents.Label {
                                text: full.money(modelData.cost)
                                opacity: 0.6
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 3.5
                                horizontalAlignment: Text.AlignRight
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }
        }

        // ---- footer ----
        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.Label {
                visible: full.stale
                text: i18n("⚠ Data is stale — is poketokend running?")
                color: full.ctpPeach
            }

            PlasmaComponents.Label {
                visible: !full.stale && text.length > 0
                opacity: 0.6
                text: {
                    if (!root.appState || !root.appState.updated_at)
                        return "";
                    var age = Math.round(Date.now() / 1000 - root.appState.updated_at);
                    if (age < 60)
                        return i18n("Updated just now");
                    return i18n("Updated %1 min ago", Math.round(age / 60));
                }
            }

            QQC2.ToolButton {
                icon.name: "view-refresh"
                text: i18n("Refresh")
                display: QQC2.AbstractButton.TextBesideIcon
                onClicked: runner.run("poketokenctl refresh")
            }

            // Show / hide the floating pet without opening Settings (upstream #302).
            QQC2.ToolButton {
                readonly property bool petOn: !!(root.appState && root.appState.settings
                                                 && root.appState.settings.floating_pet_enabled)
                text: "🐾"
                checkable: true
                checked: petOn
                onClicked: runner.run("poketokenctl set floating_pet_enabled " + (petOn ? "false" : "true"))
                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.text: petOn ? i18n("Hide floating pet") : i18n("Show floating pet")
            }

            QQC2.ToolButton {
                icon.name: "configure"
                text: i18n("Settings")
                display: QQC2.AbstractButton.IconOnly
                // Same dialog as right-click > Configure, but discoverable.
                onClicked: Plasmoid.internalAction("configure").trigger()

                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.text: i18n("Settings")
            }

            Item { Layout.fillWidth: true }

            PlasmaComponents.Label {
                visible: text.length > 0
                text: root.stateError.length > 0
                      ? root.stateError
                      : (root.appState && root.appState.errors.length > 0
                         ? root.appState.errors.join(", ")
                         : "")
                color: full.ctpRed
                elide: Text.ElideRight
                Layout.maximumWidth: full.width / 2
            }
        }
    }
}
