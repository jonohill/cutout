#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<EOF
usage: $0 [--title TITLE] [--delay DELAY] <feed_url> <base_url>
       delay format: <n>[dwmy] (e.g. 2w, 1y)
EOF
    exit 1
}

title=
delay=
positional=()
while [ $# -gt 0 ]; do
    case $1 in
        --title)
            [ $# -ge 2 ] || usage
            title=$2
            shift 2
            ;;
        --title=*)
            title=${1#--title=}
            shift
            ;;
        --delay)
            [ $# -ge 2 ] || usage
            delay=$2
            shift 2
            ;;
        --delay=*)
            delay=${1#--delay=}
            shift
            ;;
        -h|--help)
            usage
            ;;
        --)
            shift
            positional+=("$@")
            break
            ;;
        -*)
            echo "unknown option: $1" >&2
            usage
            ;;
        *)
            positional+=("$1")
            shift
            ;;
    esac
done

if [ ${#positional[@]} -lt 2 ]; then
    usage
fi

feed_url=${positional[0]}
base_url=${positional[1]}

payload=$(jq -n \
    --arg feed_url "$feed_url" \
    --arg title "$title" \
    --arg delay "$delay" \
    '{feed_url: $feed_url}
     + (if $title != "" then {title: $title} else {} end)
     + (if $delay != "" then {delay: $delay} else {} end)')
response=$(curl -fsS -X POST "${base_url%/}/podcast" \
    -H 'content-type: application/json' \
    -d "$payload")
echo "$response"
feed_id=$(echo "$response" | jq -r '.feed_id')
echo "${base_url%/}/podcast/${feed_id}"
