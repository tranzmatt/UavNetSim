import { Crosshair, Route } from 'lucide-react'

function Field({ label, children }) {
  return <label className="field"><span>{label}</span>{children}</label>
}

function PointEditor({ label, point, active, onActivate, onChange }) {
  return (
    <div className={`point-editor ${active ? 'active' : ''}`}>
      <div className="point-editor__heading">
        <span>{label}</span>
        <button type="button" className="icon-button" title={`Pick ${label.toLowerCase()} in scene`} onClick={onActivate}><Crosshair size={14} /></button>
      </div>
      <div className="coordinate-grid">
        {['x', 'y', 'z'].map((axis) => <Field key={axis} label={axis.toUpperCase()}><input type="number" step="1" value={point[axis]} onChange={(event) => onChange(axis, Number(event.target.value))} /></Field>)}
      </div>
    </div>
  )
}

function DynamicParameter({ name, definition, value, onChange }) {
  if (definition.type === 'boolean') {
    return (
      <label className="toggle-field planner-toggle">
        <span>{definition.label}</span>
        <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(name, event.target.checked)} />
        <i aria-hidden="true" />
      </label>
    )
  }
  if (definition.type === 'select') {
    return <Field label={definition.label}><select value={value} onChange={(event) => onChange(name, event.target.value)}>{definition.options.map((option) => <option key={option} value={option}>{option}</option>)}</select></Field>
  }
  return (
    <Field label={definition.label}>
      <div className="input-unit">
        <input type="number" min={definition.minimum} max={definition.maximum} step={definition.step || 1} value={value} onChange={(event) => onChange(name, Number(event.target.value))} />
        {definition.unit && <span>{definition.unit}</span>}
      </div>
    </Field>
  )
}

export default function PlanningControls({ planners, settings, activePick, planning, onSettings, onPoint, onParameter, onPick, onPlan }) {
  const planner = planners.find((item) => item.id === settings.planner_id)
  const parameters = planner?.parameters || {}
  return (
    <div className="planning-controls">
      <section className="planning-section">
        <div className="planning-section__heading"><span><small>01</small>MISSION</span><b>ENU</b></div>
        <div className="planning-section__body">
          <div className="pick-mode">
            <span>{activePick ? `Click the scene to set ${activePick}` : 'Select a point tool, then click the scene'}</span>
            <Crosshair size={14} />
          </div>
          <PointEditor label="Start" point={settings.start} active={activePick === 'start'} onActivate={() => onPick('start')} onChange={(axis, value) => onPoint('start', axis, value)} />
          <PointEditor label="Goal" point={settings.goal} active={activePick === 'goal'} onActivate={() => onPick('goal')} onChange={(axis, value) => onPoint('goal', axis, value)} />
          <div className="field-row">
            <Field label="UAV speed"><div className="input-unit"><input type="number" min="0.1" max="100" step="0.5" value={settings.uav_speed_mps} onChange={(event) => onSettings('uav_speed_mps', Number(event.target.value))} /><span>m/s</span></div></Field>
            <Field label="Safety clearance"><div className="input-unit"><input type="number" min="0" max="1000" step="0.5" value={settings.safety_clearance_m} onChange={(event) => onSettings('safety_clearance_m', Number(event.target.value))} /><span>m</span></div></Field>
          </div>
          <div className="field-row">
            <Field label="Min altitude"><div className="input-unit"><input type="number" min="0" max="10000" step="1" value={settings.min_altitude_m} onChange={(event) => onSettings('min_altitude_m', Number(event.target.value))} /><span>m</span></div></Field>
            <Field label="Max altitude"><div className="input-unit"><input type="number" min="0.1" max="10000" step="1" value={settings.max_altitude_m} onChange={(event) => onSettings('max_altitude_m', Number(event.target.value))} /><span>m</span></div></Field>
          </div>
        </div>
      </section>

      <section className="planning-section">
        <div className="planning-section__heading"><span><small>02</small>PLANNER</span><Route size={14} /></div>
        <div className="planning-section__body">
          <Field label="Algorithm"><select value={settings.planner_id} onChange={(event) => onSettings('planner_id', event.target.value)}>{planners.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
          {planner && <p className="planner-description">{planner.description}</p>}
          <div className="planner-parameters">
            {Object.entries(parameters).map(([name, definition]) => <DynamicParameter key={name} name={name} definition={definition} value={settings.parameter_values[settings.planner_id]?.[name] ?? definition.default} onChange={onParameter} />)}
          </div>
          <button type="button" className="primary-button plan-button" disabled={planning} onClick={onPlan}><Route size={16} />{planning ? 'Planning...' : 'Plan trajectory'}</button>
        </div>
      </section>
    </div>
  )
}
