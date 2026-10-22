/**
 * Lightweight "Aurora" background in the spirit of react-bits: react-bits ships
 * copy-paste components (often pulling three.js / gsap), so for a dashboard we
 * port the visual idea with pure CSS transforms instead of adding runtime weight.
 */
export function Aurora() {
  return (
    <div className="aurora" aria-hidden="true">
      <span className="aurora-blob aurora-a" />
      <span className="aurora-blob aurora-b" />
      <span className="aurora-blob aurora-c" />
      <span className="aurora-grid" />
    </div>
  )
}
