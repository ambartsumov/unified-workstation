# SUW shell integration — sourced from the managed block in ~/.zshrc / ~/.bashrc.
# Works in zsh and bash. Adds nothing to the prompt and overrides no existing command.

if [ -n "${ZSH_VERSION:-}" ]; then
    _suw_self="${(%):-%x}"
else
    _suw_self="${BASH_SOURCE[0]}"
fi
SUW_DOTFILES="$(cd "$(dirname "$_suw_self")/.." 2>/dev/null && pwd)"
unset _suw_self

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
esac

. "$SUW_DOTFILES/shell/common/aliases.sh"
case "$(uname -s)" in
    Darwin) . "$SUW_DOTFILES/shell/macos/env.sh" ;;
    *) . "$SUW_DOTFILES/shell/linux/env.sh" ;;
esac
