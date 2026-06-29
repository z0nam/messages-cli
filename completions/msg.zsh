#compdef msg messages
# zsh completion for the `msg`/`messages` CLI.
# Install: source this file from ~/.zshrc, e.g.
#   source ~/dev/messages-cli/completions/msg.zsh
#
# Subcommands complete first; for `msg read <TAB>` it offers contact names and
# chat display names (substring match) via `msg complete`. Names with spaces are
# offered whole and inserted quoted.

_msg_cli() {
  if (( CURRENT == 2 )); then
    compadd threads read unread search send reply write draft complete
    return
  fi
  # complete the recipient/identifier (the word right after read/send/reply)
  if (( CURRENT == 3 )) && [[ ${words[2]} == (read|send|reply|write|draft) ]]; then
    local cur=${words[CURRENT]}
    # A mid-composition Korean syllable (밑줄 표시, 미확정) reaches zsh as an
    # EMPTY word. Without this guard we'd ask msg for an empty prefix and dump
    # all ~2000 candidates (and -U would wipe the input). Bail instead — commit
    # the syllable first (밑줄이 사라진 뒤) then Tab.
    [[ -z $cur ]] && return
    local -a cands
    cands=("${(@f)$(command msg complete -- "$cur" 2>/dev/null)}")
    (( ${#cands} )) || return
    # Safety backstop against a pathological flood (the real culprit, an empty
    # query, is already guarded above; a common surname like 김 ~281 is fine).
    (( ${#cands} > 500 )) && return
    # -U: we already filtered, so offer all candidates regardless of prefix
    # position (lets "길동"<TAB> complete to "홍길동").
    compadd -U -- "${cands[@]}"
    return
  fi
}

compdef _msg_cli msg messages
