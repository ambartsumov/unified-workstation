# Design

**Scene.** Someone sits down at a desk with a second laptop beside the first, in whatever light
the room has, and glances at this window between two other tasks. It sits next to the operating
system's own settings — so it follows the system's light or dark appearance instead of
imposing one, and both are designed.

**Colour strategy: restrained.** Neutrals carry the interface, tinted very slightly (chroma
≤ 0.008) toward the brand hue. One accent — petrol, OKLCH hue 225 — marks the primary action
and the current place, on well under a tenth of any screen. Status colours (in sync, attention,
error) are reserved for status and always accompanied by a symbol and a word. All colours are
OKLCH tokens in `suw/app/static/app.css`; body text meets 4.5:1 in both themes.

**Type.** The system interface font in three weights; the system monospace for paths, codes
and fingerprints. No web fonts: the window must work offline and loads nothing from the network.
Prose is capped at 68ch.

**Layout.** A fixed navigation rail and one content column. Status is a list of rows (symbol,
name, state, one action), not a grid of cards. Borders define surfaces; shadows are kept for
things that float (dialogs, toasts). Corner radius 6–10px.

**Motion.** 140 ms, ease-out-quart, opacity and a 4px rise when a page changes; nothing else
moves. Disabled under `prefers-reduced-motion`.

**Interaction.** Native `<dialog>` for every modal. Everything reachable by keyboard with a
visible focus ring. Destructive or ambiguous actions say exactly what will change and need an
explicit confirmation. Errors are answers: what happened, why, what you can do.
