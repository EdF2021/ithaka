// static/js/uiZoom.js
//
// Compensates position:fixed, body-portaled popups (built from
// getBoundingClientRect() measurements — kebab/overflow menus, context
// popups, dropdowns) for the UI text-scale zoom (`:root.ui-scale-125 {
// zoom: 1.25 }`, toggled in Settings). getBoundingClientRect() and
// window.innerWidth/innerHeight report real viewport px, but any px value
// JS assigns to style.top/left/right/bottom on an element renders
// re-multiplied by the effective zoom of its ancestors up to :root (the
// popup lives inside the zoomed root whether portaled to <body> or not).
// Divide viewport-space measurements by the zoom before assigning them as
// local style values so set-px and rendered-px line up again.
//
// offsetHeight/offsetWidth/scrollHeight/scrollWidth are already reported in
// the element's own local (unzoomed) px — do NOT divide those. Only
// getBoundingClientRect() results and window.innerWidth/innerHeight need
// conversion. See PR #76 (composer plus-menu) and #77 (this module).
//
// Split into a pure math half (toLocalPx, unit-tested under plain node) and
// a DOM-touching half (zoomOf) that isn't — same spirit as escMenuStack.js.

/** Effective CSS zoom of `el` (1 when unset, unsupported, or `el` is falsy —
 * non-zoomed browsers and the default 100% UI scale both read as 1).
 *
 * Callers should pass `document.documentElement` (:root carries the
 * `ui-scale-125` class, and the app never resets `zoom` on any descendant —
 * see style.css), NOT a popup that was just created and appended: a
 * freshly-inserted node's `currentCSSZoom` can still read stale/1 until the
 * browser has run a style/layout pass on it, which caused a real duplicate-
 * scaling bug during #77 (export menu off by the un-divided pixel amount). */
export function zoomOf(el) {
  return (el && el.currentCSSZoom) || 1;
}

/** Convert a viewport-space px value (from getBoundingClientRect(),
 * window.innerWidth/innerHeight, or a MouseEvent's clientX/Y) into the local
 * px value to assign to a fixed element's style.top/left/right/bottom so it
 * renders at the intended viewport position under the given zoom. */
export function toLocalPx(viewportPx, zoom) {
  return viewportPx / (zoom || 1);
}
