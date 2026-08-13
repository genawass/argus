# TAM login nudge — append to ~/.bashrc to see the morning headline on login.
#
#   cat /home/user/dev/argus/deploy/bashrc-nudge.sh >> ~/.bashrc
#
# `tam nudge` prints one line when something is overdue, due today or blocked,
# and prints nothing at all once the day's review is closed out. All the logic
# lives in the CLI, so this stays a two-line shell function.

tam_nudge() {
    case $- in *i*) ;; *) return ;; esac    # interactive shells only
    [ -x "$HOME/dev/argus/bin/tam" ] || return
    "$HOME/dev/argus/bin/tam" nudge 2>/dev/null
}
tam_nudge
