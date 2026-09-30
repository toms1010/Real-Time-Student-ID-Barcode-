/** Live camera preview with the scanner HUD. */

import { useEffect, useState } from 'react'

import { formatMs } from '@/utils/format'
import { errorMessage, isScanning } from '@/utils/ui'
import type { ScannerState } from '@/types'

export function CameraView({
  state,
  onToggle,
  busy,
}: {
  state: ScannerState | null
  onToggle?: () => void
  busy?: boolean
}) {
  const [streamKey, setStreamKey] = useState(() => Date.now())
  const [streamError, setStreamError] = useState(false)

  // Restart the MJPEG stream when the user toggles the scanner, so the browser
  // opens a new connection instead of replaying a dead one.
  useEffect(() => {
    setStreamKey(Date.now())
    setStreamError(false)
  }, [state?.camera_connected, state?.stream_source])

  const connected = state?.camera_connected ?? false
  const scanning = isScanning(state?.state)

  return (
    <div className="camera-frame">
      {connected ? (
        <img
          key={streamKey}
          src={`/api/stream/mjpeg?t=${streamKey}`}
          alt="Live camera preview with the barcode scan region highlighted"
          onError={() => setStreamError(true)}
        />
      ) : (
        <div className="camera-placeholder">
          <div className="big">📷</div>
          <strong>Camera unavailable</strong>
          <p className="small" style={{ maxWidth: 420 }}>
            {state?.error_message ??
              errorMessage(state?.error_code, 'Please check the webcam connection.')}
          </p>
          {state?.error_code ? (
            <p className="mono dim small">error code: {state.error_code}</p>
          ) : null}
        </div>
      )}

      {streamError && connected ? (
        <div className="camera-placeholder">
          <div className="big">⚠</div>
          <strong>Preview interrupted</strong>
          <button type="button" className="btn small" onClick={() => setStreamKey(Date.now())}>
            Reconnect
          </button>
        </div>
      ) : null}

      <div className="camera-overlay">
        <div className="camera-hud">
          <div>
            state: <strong>{state?.state ?? 'UNKNOWN'}</strong>
          </div>
          <div>
            fps: {state?.fps?.toFixed?.(1) ?? '—'} · frame: {formatMs(state?.avg_processing_ms)} ·
            detect: {formatMs(state?.avg_detection_ms)} · db: {formatMs(state?.avg_database_ms)}
          </div>
          <div>
            device: {state?.camera_device ?? '—'} · frames: {state?.frames_processed ?? 0} ·
            cooldowns: {state?.cooldowns_active ?? 0}
          </div>
        </div>
        <div className="flex-row">
          <span className="camera-hud" style={{ color: scanning ? '#7ef2b8' : '#9fb2cc' }}>
            {scanning ? '● scanning' : '○ idle'}
          </span>
          {onToggle ? (
            <button type="button" className="btn small" onClick={onToggle} disabled={busy}>
              {busy ? <span className="spinner" /> : null}
              {scanning || connected ? 'Stop camera' : 'Start camera'}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
