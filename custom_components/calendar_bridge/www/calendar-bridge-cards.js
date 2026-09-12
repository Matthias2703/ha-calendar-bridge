/**
 * Calendar Bridge -- "Create event" Lovelace cards.
 *
 * Two card types sharing one implementation:
 *   - calendar-bridge-create-card-mobile  (single column, compact)
 *   - calendar-bridge-create-card-tablet  (two columns, shows the reminder
 *     method field and starts with "Weitere Angaben" expanded)
 *
 * Pure frontend: the calendar picker is built entirely from the connected
 * hass object's entity/device registries (every entity calendar_bridge
 * creates carries `platform: "calendar_bridge"`), so no new backend API is
 * needed -- the card just calls the existing calendar_bridge.create_event
 * service with a device_id, exactly like the create_event service UI does.
 */

const REMINDER_OPTIONS = [
  { value: "", label: "Keine" },
  { value: "5", label: "5 Minuten vorher" },
  { value: "15", label: "15 Minuten vorher" },
  { value: "30", label: "30 Minuten vorher" },
  { value: "60", label: "1 Stunde vorher" },
  { value: "1440", label: "1 Tag vorher" },
];

const METHOD_OPTIONS = [
  { value: "popup", label: "Popup" },
  { value: "email", label: "E-Mail" },
];

const RRULE_OPTIONS = [
  { value: "", label: "Keine" },
  { value: "FREQ=DAILY", label: "Täglich" },
  { value: "FREQ=WEEKLY", label: "Wöchentlich" },
  { value: "FREQ=MONTHLY", label: "Monatlich" },
  { value: "FREQ=YEARLY", label: "Jährlich" },
];

const CARD_CSS = `
  :host { display: block; }
  ha-card { padding: 16px; }
  .header { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
  .header ha-icon { color: var(--primary-color); }
  .header span { font-size: 1.3em; font-weight: 500; color: var(--primary-text-color); }
  .row { margin-bottom: 12px; }
  .row label { display: block; font-size: 0.85em; color: var(--secondary-text-color); margin-bottom: 4px; }
  .row select, .row input[type="text"], .row input[type="date"], .row input[type="time"], .row textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color);
    color: var(--primary-text-color);
    font-size: 1em;
    font-family: inherit;
  }
  .row textarea { min-height: 64px; resize: vertical; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
  .toggle-row { display: flex; align-items: center; justify-content: space-between; }
  .toggle-row label { margin-bottom: 0; }
  ha-switch { --mdc-theme-secondary: var(--primary-color); }
  .more { border-top: 1px solid var(--divider-color); padding-top: 8px; margin-top: 4px; margin-bottom: 12px; }
  .more summary { cursor: pointer; display: flex; align-items: center; gap: 4px; color: var(--primary-text-color); padding: 8px 0; list-style: none; }
  .more summary::-webkit-details-marker { display: none; }
  .more summary ha-icon { transition: transform 0.2s; }
  .more[open] summary ha-icon { transform: rotate(180deg); }
  .more .more-fields { padding-top: 8px; }
  mwc-button.submit, button.submit {
    width: 100%;
    padding: 12px;
    border: none;
    border-radius: 8px;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
    font-size: 1em;
    font-weight: 500;
    cursor: pointer;
  }
  button.submit:disabled { opacity: 0.6; cursor: default; }
  .footnote { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 0.82em; color: var(--secondary-text-color); }
  .footnote ha-icon { --mdc-icon-size: 16px; color: var(--info-color, var(--primary-color)); }
  .error { color: var(--error-color); font-size: 0.85em; margin-top: 8px; }
`;

class CalendarBridgeCreateCardBase extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  get _showMethod() {
    return false;
  }

  get _defaultExpanded() {
    return false;
  }

  setConfig(config) {
    this._config = config || {};
    this._state = {
      device_id: "",
      summary: "",
      date: "",
      all_day: false,
      start: "09:00",
      end: "09:30",
      reminder_minutes: "30",
      method: "popup",
      rrule: "",
      location: "",
      description: "",
    };
    this._expanded = this._defaultExpanded;
    this._submitting = false;
    this._error = null;
    this._rendered = false;
    this._deviceOptionsKey = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) {
      this._buildDom();
      this._rendered = true;
    }
    this._refreshDeviceOptions();
  }

  getCardSize() {
    return this._showMethod ? 7 : 6;
  }

  // --- device discovery, purely from the frontend registries ---

  _calendarDevices() {
    const hass = this._hass;
    if (!hass || !hass.entities || !hass.devices) return [];
    const deviceIds = new Set();
    for (const entityId in hass.entities) {
      const ent = hass.entities[entityId];
      if (ent && ent.platform === "calendar_bridge" && ent.device_id) {
        deviceIds.add(ent.device_id);
      }
    }
    const result = [];
    deviceIds.forEach((id) => {
      const device = hass.devices[id];
      if (device) {
        result.push({ id, name: device.name_by_user || device.name || id });
      }
    });
    result.sort((a, b) => a.name.localeCompare(b.name));
    return result;
  }

  _refreshDeviceOptions() {
    const devices = this._calendarDevices();
    const key = devices.map((d) => `${d.id}:${d.name}`).join("|");
    if (key === this._deviceOptionsKey) return;
    this._deviceOptionsKey = key;
    const select = this._root.querySelector("#calendar");
    if (!select) return;
    const previous = this._state.device_id;
    select.innerHTML =
      devices.length === 0
        ? '<option value="">Kein Calendar-Bridge-Kalender gefunden</option>'
        : devices
            .map((d) => `<option value="${d.id}">${this._escape(d.name)}</option>`)
            .join("");
    if (devices.some((d) => d.id === previous)) {
      select.value = previous;
    } else if (devices.length > 0) {
      this._state.device_id = devices[0].id;
      select.value = devices[0].id;
    }
  }

  _escape(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Same channel the rest of the HA frontend uses for the bottom-left
  // snackbar (e.g. "Turned on", "Scene activated") -- no toast UI of our
  // own to build or keep in sync with the current theme.
  _notify(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      })
    );
  }

  // --- rendering ---

  _buildDom() {
    this._root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CARD_CSS;
    this._root.appendChild(style);

    const card = document.createElement("ha-card");
    card.innerHTML = this._template();
    this._root.appendChild(card);

    this._wireEvents();
  }

  _template() {
    const s = this._state;
    const timeRow = s.all_day
      ? ""
      : this._showMethod
        ? `<div class="grid2">
             <div class="row"><label for="start">Beginn</label><input type="time" id="start" value="${s.start}"></div>
             <div class="row"><label for="end">Ende</label><input type="time" id="end" value="${s.end}"></div>
           </div>`
        : `<div class="row"><label for="start">Beginn</label><input type="time" id="start" value="${s.start}"></div>
           <div class="row"><label for="end">Ende</label><input type="time" id="end" value="${s.end}"></div>`;

    const reminderRow = this._showMethod
      ? `<div class="grid2">
           <div class="row"><label for="reminder">Erinnerung</label>${this._select("reminder", REMINDER_OPTIONS, s.reminder_minutes)}</div>
           <div class="row"><label for="method">Methode</label>${this._select("method", METHOD_OPTIONS, s.method)}</div>
         </div>`
      : `<div class="row"><label for="reminder">Erinnerung</label>${this._select("reminder", REMINDER_OPTIONS, s.reminder_minutes)}</div>`;

    return `
      <div class="header"><ha-icon icon="mdi:calendar-plus"></ha-icon><span>Termin anlegen</span></div>
      <div class="row"><label for="calendar">Kalender</label><select id="calendar"></select></div>
      <div class="row"><label for="summary">Titel</label><input type="text" id="summary" placeholder="z. B. Zahnarzt" value="${this._escape(s.summary)}"></div>
      <div class="grid2">
        <div class="row"><label for="date">Datum</label><input type="date" id="date" value="${s.date}"></div>
        <div class="row toggle-row"><label for="all_day">Ganztägig</label><ha-switch id="all_day" ${s.all_day ? "checked" : ""}></ha-switch></div>
      </div>
      ${timeRow}
      ${reminderRow}
      <div class="row"><label for="rrule">Wiederholung</label>${this._select("rrule", RRULE_OPTIONS, s.rrule)}</div>
      <details class="more" ${this._expanded ? "open" : ""}>
        <summary><ha-icon icon="mdi:chevron-down"></ha-icon> Weitere Angaben</summary>
        <div class="more-fields">
          <div class="row"><label for="location">Ort</label><input type="text" id="location" value="${this._escape(s.location)}"></div>
          <div class="row"><label for="description">Beschreibung</label><textarea id="description">${this._escape(s.description)}</textarea></div>
        </div>
      </details>
      <button class="submit" id="submit">Termin erstellen</button>
      ${this._error ? `<div class="error">${this._escape(this._error)}</div>` : ""}
      <div class="footnote"><ha-icon icon="mdi:information-outline"></ha-icon><span>Erinnerung wird im Kalender gespeichert.</span></div>
    `;
  }

  _select(id, options, current) {
    return `<select id="${id}">${options
      .map(
        (o) =>
          `<option value="${o.value}" ${o.value === current ? "selected" : ""}>${o.label}</option>`
      )
      .join("")}</select>`;
  }

  _rerenderPreservingFocus() {
    const card = this._root.querySelector("ha-card");
    card.innerHTML = this._template();
    this._wireEvents();
    this._deviceOptionsKey = "";
    this._refreshDeviceOptions();
  }

  _wireEvents() {
    const $ = (id) => this._root.querySelector("#" + id);
    const on = (id, ev, handler) => {
      const el = $(id);
      if (el) el.addEventListener(ev, handler);
    };

    on("calendar", "change", (e) => (this._state.device_id = e.target.value));
    on("summary", "input", (e) => (this._state.summary = e.target.value));
    on("date", "change", (e) => (this._state.date = e.target.value));
    on("start", "change", (e) => (this._state.start = e.target.value));
    on("end", "change", (e) => (this._state.end = e.target.value));
    on("reminder", "change", (e) => (this._state.reminder_minutes = e.target.value));
    on("method", "change", (e) => (this._state.method = e.target.value));
    on("rrule", "change", (e) => (this._state.rrule = e.target.value));
    on("location", "input", (e) => (this._state.location = e.target.value));
    on("description", "input", (e) => (this._state.description = e.target.value));
    on("all_day", "change", (e) => {
      this._state.all_day = e.target.checked;
      this._rerenderPreservingFocus();
    });
    const details = this._root.querySelector(".more");
    if (details) {
      details.addEventListener("toggle", () => {
        this._expanded = details.open;
      });
    }
    on("submit", "click", () => this._submit());
  }

  // --- submit ---

  _composeDateTime(date, time) {
    if (!date) return null;
    return time ? `${date}T${time}:00` : date;
  }

  async _submit() {
    const s = this._state;
    this._error = null;

    if (!s.device_id) {
      this._error = "Bitte einen Kalender auswählen.";
      this._rerenderPreservingFocus();
      return;
    }
    if (!s.summary.trim()) {
      this._error = "Bitte einen Titel eingeben.";
      this._rerenderPreservingFocus();
      return;
    }
    const start = this._composeDateTime(s.date, s.all_day ? null : s.start);
    if (!start) {
      this._error = "Bitte ein Datum auswählen.";
      this._rerenderPreservingFocus();
      return;
    }
    const end = s.all_day ? null : this._composeDateTime(s.date, s.end);

    const payload = {
      device_id: [s.device_id],
      summary: s.summary.trim(),
      start,
      all_day: s.all_day,
    };
    if (end) payload.end = end;
    if (s.location.trim()) payload.location = s.location.trim();
    if (s.description.trim()) payload.description = s.description.trim();
    if (s.rrule) payload.rrule = s.rrule;
    if (s.reminder_minutes) {
      if (this._showMethod) {
        payload.reminders = [{ minutes_before: parseInt(s.reminder_minutes, 10), method: s.method }];
      } else {
        payload.reminder_minutes = parseInt(s.reminder_minutes, 10);
      }
    }

    const submitBtn = this._root.querySelector("#submit");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Wird erstellt …";
    }
    try {
      await this._hass.callService("calendar_bridge", "create_event", payload);
      this._notify(`Termin "${s.summary.trim()}" wurde erstellt.`);
      this._state = {
        ...this._state,
        summary: "",
        date: "",
        location: "",
        description: "",
        rrule: "",
      };
      this._rerenderPreservingFocus();
    } catch (err) {
      this._error = (err && err.message) || "Termin konnte nicht erstellt werden.";
      this._rerenderPreservingFocus();
    }
  }
}

class CalendarBridgeCreateCardMobile extends CalendarBridgeCreateCardBase {
  get _showMethod() {
    return false;
  }

  get _defaultExpanded() {
    return false;
  }
}

class CalendarBridgeCreateCardTablet extends CalendarBridgeCreateCardBase {
  get _showMethod() {
    return true;
  }

  get _defaultExpanded() {
    return true;
  }
}

customElements.define("calendar-bridge-create-card-mobile", CalendarBridgeCreateCardMobile);
customElements.define("calendar-bridge-create-card-tablet", CalendarBridgeCreateCardTablet);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "calendar-bridge-create-card-mobile",
    name: "Calendar Bridge: Termin anlegen (Handy)",
    description: "Kompaktes Formular zum Anlegen eines Kalendertermins über Calendar Bridge.",
  },
  {
    type: "calendar-bridge-create-card-tablet",
    name: "Calendar Bridge: Termin anlegen (Tablet)",
    description: "Zweispaltiges Formular zum Anlegen eines Kalendertermins über Calendar Bridge, inkl. Erinnerungsmethode.",
  }
);
