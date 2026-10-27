#!/usr/bin/env bash
# ==============================================================================
# audio_ctl.sh - Manage PipeWire virtual sink for Discord Speaker on Arch Linux
# ==============================================================================

SINK_NAME="${SINK_NAME:-DiscordSpeaker}"
SINK_DESC="Discord Speaker"
LOOPBACK_DESC="Discord_Speaker_Monitoring"

is_sink_loaded() {
    pactl list short sinks | awk -v name="$SINK_NAME" '$2 == name { found=1 } END { exit !found }'
}

is_loopback_loaded() {
    pactl list short modules | grep -E "module-loopback.*source=${SINK_NAME}\.monitor" > /dev/null 2>&1
}

cmd_setup() {
    echo "==> Configuring PipeWire audio sink..."
    if is_sink_loaded; then
        echo "  [OK] Sink '${SINK_NAME}' is already active."
    else
        SINK_ID=$(pactl load-module module-null-sink \
            sink_name="${SINK_NAME}" \
            sink_properties="device.description='${SINK_DESC}'")
        if [ $? -eq 0 ]; then
            echo "  [OK] Created null sink '${SINK_NAME}' (Module ID: ${SINK_ID})"
        else
            echo "  [ERROR] Failed to create null sink '${SINK_NAME}'" >&2
            return 1
        fi
    fi

    if [ "$1" = "--loopback" ]; then
        cmd_enable_loopback
    fi
}

cmd_teardown() {
    echo "==> Tearing down PipeWire audio configuration..."
    # First unload any loopback
    cmd_disable_loopback

    # Unload sink
    local sink_modules
    sink_modules=$(pactl list short modules | grep "module-null-sink" | grep "${SINK_NAME}" | awk '{print $1}')
    if [ -n "$sink_modules" ]; then
        for mod_id in $sink_modules; do
            pactl unload-module "$mod_id"
            echo "  [OK] Unloaded sink module ${mod_id}"
        done
    else
        echo "  [INFO] No active sink module found for '${SINK_NAME}'."
    fi
}

cmd_enable_loopback() {
    if ! is_sink_loaded; then
        echo "  [INFO] Sink not loaded. Setting up sink first..."
        cmd_setup
    fi

    if is_loopback_loaded; then
        echo "  [OK] Loopback monitoring is already active."
    else
        LOOP_ID=$(pactl load-module module-loopback \
            source="${SINK_NAME}.monitor" \
            sink_properties=node.description="${LOOPBACK_DESC}")
        if [ $? -eq 0 ]; then
            echo "  [OK] Enabled local loopback monitoring (Module ID: ${LOOP_ID})"
        else
            echo "  [ERROR] Failed to load loopback module" >&2
            return 1
        fi
    fi
}

cmd_disable_loopback() {
    local loop_modules
    loop_modules=$(pactl list short modules | grep "module-loopback" | grep "${SINK_NAME}\.monitor" | awk '{print $1}')
    if [ -n "$loop_modules" ]; then
        for mod_id in $loop_modules; do
            pactl unload-module "$mod_id"
            echo "  [OK] Unloaded loopback module ${mod_id}"
        done
    else
        echo "  [INFO] No active loopback monitoring found."
    fi
}

cmd_status() {
    echo "================ PipeWire Audio Status ================"
    if is_sink_loaded; then
        echo "Sink (${SINK_NAME}): ACTIVE"
        echo "Monitor Source: ${SINK_NAME}.monitor"
    else
        echo "Sink (${SINK_NAME}): NOT ACTIVE (run './audio_ctl.sh setup')"
    fi

    if is_loopback_loaded; then
        echo "Local Loopback Monitoring: ACTIVE"
    else
        echo "Local Loopback Monitoring: INACTIVE (audio streams only to Discord)"
    fi

    echo ""
    echo "--- Applications currently routed to ${SINK_NAME} ---"
    local found=0
    local sink_id
    sink_id=$(pactl list short sinks | awk -v name="$SINK_NAME" '$2 == name {print $1; exit}')
    # Query sink inputs
    while IFS= read -r line; do
        if [[ "$line" =~ Sink\ Input\ #([0-9]+) ]]; then
            current_input="${BASH_REMATCH[1]}"
            current_app=""
            current_sink=""
        elif [[ "$line" =~ Sink:\ ([0-9]+|${SINK_NAME}) ]]; then
            current_sink="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ application\.name\ =\ \"([^\"]+)\" ]]; then
            current_app="${BASH_REMATCH[1]}"
            # Check if this input belongs to our sink
            if [ -n "$sink_id" ] && [ "$current_sink" = "$sink_id" ]; then
                echo "  • [Sink Input #${current_input}] ${current_app}"
                found=1
            fi
        fi
    done < <(pactl list sink-inputs 2>/dev/null)

    if [ $found -eq 0 ]; then
        echo "  (None. To route Spotify, use './audio_ctl.sh route-spotify' or pavucontrol)"
    fi
    echo "======================================================="
}

cmd_route_spotify() {
    if ! is_sink_loaded; then
        echo "Sink '${SINK_NAME}' is not loaded. Loading now..."
        cmd_setup
    fi

    echo "==> Searching for active Spotify streams..."
    local routed=0
    local input_id=""
    local app_name=""

    # Parse pactl sink-inputs
    while IFS= read -r line; do
        if [[ "$line" =~ Sink\ Input\ #([0-9]+) ]]; then
            input_id="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ application\.name\ =\ \"([^\"]+)\" ]] || [[ "$line" =~ application\.process\.binary\ =\ \"([^\"]+)\" ]]; then
            app_name="${BASH_REMATCH[1]}"
            if [[ "${app_name,,}" =~ spotify ]]; then
                echo "  Found Spotify stream: Input #${input_id} (${app_name})"
                if pactl move-sink-input "${input_id}" "${SINK_NAME}"; then
                    echo "  [OK] Successfully moved Spotify stream to '${SINK_NAME}'!"
                    routed=1
                else
                    echo "  [ERROR] Failed to move stream #${input_id}" >&2
                fi
            fi
        fi
    done < <(pactl list sink-inputs 2>/dev/null)

    if [ $routed -eq 0 ]; then
        echo "  [WARNING] No active Spotify playback stream was detected."
        echo "  Tip: Make sure Spotify is playing music, then run this command again,"
        echo "  or open 'pavucontrol' and set Spotify's output to '${SINK_DESC}'."
    fi
}

cmd_route_app() {
    local target_app="$1"
    if [ -z "$target_app" ]; then
        echo "Usage: $0 route-app <application_name>"
        return 1
    fi

    if ! is_sink_loaded; then
        cmd_setup
    fi

    echo "==> Searching for active '${target_app}' streams..."
    local routed=0
    local input_id=""
    local app_name=""

    while IFS= read -r line; do
        if [[ "$line" =~ Sink\ Input\ #([0-9]+) ]]; then
            input_id="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ application\.name\ =\ \"([^\"]+)\" ]] || [[ "$line" =~ application\.process\.binary\ =\ \"([^\"]+)\" ]]; then
            app_name="${BASH_REMATCH[1]}"
            if [[ "${app_name,,}" =~ ${target_app,,} ]]; then
                echo "  Found match: Input #${input_id} (${app_name})"
                if pactl move-sink-input "${input_id}" "${SINK_NAME}"; then
                    echo "  [OK] Successfully moved stream to '${SINK_NAME}'!"
                    routed=1
                fi
            fi
        fi
    done < <(pactl list sink-inputs 2>/dev/null)

    if [ $routed -eq 0 ]; then
        echo "  [WARNING] No active stream matching '${target_app}' was found."
    fi
}

case "$1" in
    setup)
        cmd_setup "$2"
        ;;
    teardown)
        cmd_teardown
        ;;
    status)
        cmd_status
        ;;
    enable-loopback)
        cmd_enable_loopback
        ;;
    disable-loopback)
        cmd_disable_loopback
        ;;
    route-spotify)
        cmd_route_spotify
        ;;
    route-app)
        cmd_route_app "$2"
        ;;
    *)
        echo "Usage: $0 {setup|teardown|status|route-spotify|route-app <name>|enable-loopback|disable-loopback}"
        echo ""
        echo "Commands:"
        echo "  setup             Create the '${SINK_NAME}' null sink (clean, no loopback)"
        echo "  setup --loopback  Create the sink and enable headphone loopback"
        echo "  teardown          Remove the virtual sink (and loopback if active)"
        echo "  status            Show status of sink and connected apps"
        echo "  route-spotify     Move playing Spotify stream to '${SINK_NAME}'"
        echo "  route-app <name>  Move any matching app (e.g. firefox, mpv) to '${SINK_NAME}'"
        echo "  enable-loopback   Listen to '${SINK_NAME}' on your default headphones"
        echo "  disable-loopback  Turn off local headphone monitoring"
        exit 1
        ;;
esac
