import React from 'react'
import { ChevronDown } from 'lucide-react'
import styles from './TopNav.module.css'

function matchesContains(text, query) {
  if (!query.trim()) return true
  return String(text || '').toLowerCase().includes(query.trim().toLowerCase())
}

export default function NavSelect({
  value,
  onChange,
  options,
  disabled = false,
  placeholder = '— select —',
  ariaLabel,
}) {
  const [open, setOpen] = React.useState(false)
  const [query, setQuery] = React.useState('')
  const ref = React.useRef(null)
  const inputRef = React.useRef(null)

  React.useEffect(() => {
    function onOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) close()
    }
    document.addEventListener('mousedown', onOutside)
    return () => document.removeEventListener('mousedown', onOutside)
  }, [])

  React.useEffect(() => {
    if (disabled) close()
  }, [disabled])

  function close() {
    setOpen(false)
    setQuery('')
  }

  function openMenu() {
    if (disabled || open) return
    setOpen(true)
    setQuery('')
    requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
  }

  function pick(next) {
    onChange(next)
    close()
  }

  const trimmed = query.trim()
  const filtered = options.filter(opt => matchesContains(opt, trimmed))

  function onInputKeyDown(e) {
    if (e.key === 'Escape') {
      close()
      e.preventDefault()
    } else if (e.key === 'Enter' && filtered.length > 0) {
      pick(filtered[0])
      e.preventDefault()
    }
  }

  return (
    <div
      ref={ref}
      className={`${styles.navVal} ${disabled ? styles.navValDisabled : ''} ${open ? styles.navValOpen : ''}`}
    >
      <div className={styles.navSelectBtn}>
        <input
          ref={inputRef}
          type="text"
          className={`${styles.navSelectInput} ${!open && !value ? styles.navSelectInputPlaceholder : ''}`}
          value={open ? query : (value || '')}
          readOnly={!open}
          disabled={disabled}
          placeholder={placeholder}
          aria-label={ariaLabel}
          aria-expanded={open}
          aria-haspopup="listbox"
          title={value || placeholder}
          onChange={e => setQuery(e.target.value)}
          onFocus={openMenu}
          onKeyDown={onInputKeyDown}
        />
        <button
          type="button"
          className={styles.navSelectCaretBtn}
          disabled={disabled}
          aria-label={`${ariaLabel} menu`}
          tabIndex={-1}
          onMouseDown={e => e.preventDefault()}
          onClick={() => (open ? close() : openMenu())}
        >
          <ChevronDown size={13} className={`${styles.caret} ${open ? styles.caretOpen : ''}`} />
        </button>
      </div>

      {open && (
        <ul className={styles.navSelectMenu} role="listbox" aria-label={ariaLabel}>
          {!trimmed && (
            <li role="none">
              <button
                type="button"
                role="option"
                aria-selected={!value}
                className={`${styles.navSelectOption} ${!value ? styles.navSelectOptionActive : ''}`}
                onMouseDown={e => e.preventDefault()}
                onClick={() => pick('')}
              >
                {placeholder}
              </button>
            </li>
          )}
          {filtered.map(opt => (
            <li key={opt} role="none">
              <button
                type="button"
                role="option"
                aria-selected={value === opt}
                className={`${styles.navSelectOption} ${value === opt ? styles.navSelectOptionActive : ''}`}
                onMouseDown={e => e.preventDefault()}
                onClick={() => pick(opt)}
              >
                {opt}
              </button>
            </li>
          ))}
          {trimmed && filtered.length === 0 && (
            <li className={styles.navSelectEmpty} role="none">No matches</li>
          )}
        </ul>
      )}
    </div>
  )
}
