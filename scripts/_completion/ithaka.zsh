#compdef ithaka ithaka-backup ithaka-calendar ithaka-contacts ithaka-cookbook ithaka-docs ithaka-gallery ithaka-mail ithaka-mcp ithaka-memory ithaka-notes ithaka-personal ithaka-preset ithaka-research ithaka-sessions ithaka-signature ithaka-skills ithaka-tasks ithaka-theme ithaka-webhook
# Zsh tab-completion for the ithaka umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/ithaka-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `ithaka <tab>` completes subcommands; `ithaka mail <tab>`
# completes mail subcommands; `ithaka-mail <tab>` works the same.

_ithaka_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _ithaka_subs

_ithaka_refresh() {
    _ithaka_subs=()
    local dir="$(_ithaka_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/ithaka-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#ithaka-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _ithaka_subs[$sub]="$commands"
    done
}

_ithaka() {
    [[ ${#_ithaka_subs} -eq 0 ]] && _ithaka_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "ithaka" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_ithaka_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_ithaka_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_ithaka_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # ithaka-foo <tab>
    local sub="${cmd#ithaka-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_ithaka_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_ithaka "$@"
