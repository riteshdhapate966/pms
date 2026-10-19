'use strict';
let workspaceConfig = null, mappingData = null;

function activateTab(name, focus = false) {
  if (!document.querySelector(`[data-tab="${name}"]`)) name = 'properties';
  document.querySelectorAll('[data-tab]').forEach(button => {
    const active = button.dataset.tab === name;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (active && focus) button.focus();
  });
  document.querySelectorAll('.tab-view').forEach(panel => panel.classList.toggle('hidden', panel.id !== 'view-' + name));
  history.replaceState(null, '', '#' + name);
}

function dataTable(target, headers, rows, emptyText = 'No records yet.') {
  target.replaceChildren();
  if (!rows.length) { target.append(element('p', emptyText, 'empty')); return; }
  const wrap = element('div', undefined, 'table-scroll');
  const table = element('table', undefined, 'data-table');
  const head = element('thead'), tr = element('tr');
  for (const title of headers) { const th = element('th', title); th.scope = 'col'; tr.append(th); }
  head.append(tr);table.append(head);
  const body = element('tbody');
  for (const values of rows) {
    const row = element('tr');
    for (const value of values) {
      const td = element('td');
      if (value instanceof Node) td.append(value);
      else td.textContent = value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
      row.append(td);
    }
    body.append(row);
  }
  table.append(body);wrap.append(table);target.append(wrap);
}

function openBookingButton(id, label = 'Open booking') {
  const button = element('button', label, 'quiet');button.type = 'button';
  button.onclick = () => { selected = id;render();activateTab('bookings'); };
  return button;
}

function renderTabs() {
  if (!state) return;
  $('#live-create-panel').classList.toggle('hidden',state.simulation);
  document.querySelector('#view-bookings .setup').classList.toggle('hidden',!state.simulation);
  document.querySelector('#view-bookings .workspace').classList.toggle('live-workspace',!state.simulation);
  if(!state.simulation){
    document.querySelector('.intro-note strong').textContent='Live PMS workspace';
    document.querySelector('.intro-note p').textContent='Reads go to WISH. Real writes run only through configured vendor contracts. Mock records and failure controls are excluded.';
    $('#metric-bookings').previousElementSibling.textContent='Tracked live bookings';
    $('#live-create-readiness').textContent=state.capabilities.create.ready?'Create contract configured. Submitting this form sends a real PMS write.':state.capabilities.create.missing.join('; ');
    $('#view-refunds h2').textContent='External refund records';
    $('#view-refunds .hint').textContent='Only refunds already completed externally can be recorded and synchronized here. This app does not issue real payment refunds.';
    $('#view-activity .hint').textContent='Real PMS delivery events, last acknowledgements and attempt history for this workspace.';
  }
  const search = $('#property-search').value.toLowerCase().trim();
  const links = state.property_links.filter(link => [link.stay_id,link.stay_name,link.pms_property_code].some(value => value.toLowerCase().includes(search)));
  dataTable($('#property-directory'), ['Stay', 'Stay ID', 'PMS property', 'Source', 'Bookings'], links.map(link => [
    link.stay_name,link.stay_id,link.pms_property_code,link.source_id,state.bookings.filter(booking => booking.stay_id === link.stay_id).length,
  ]), search ? 'No properties match your search.' : 'No linked properties. Use Link your property to add one.');
  dataTable($('#refund-directory'), ['Booking', 'Amount', 'Payment', 'PMS sync', 'Reference', ''], state.refunds.map(refund => {
    const booking = state.bookings.find(item => item.id === refund.booking_id);
    const event = state.events.find(item => item.kind === 'refund.completed' && item.payload.refund_id === refund.id);
    return [booking?.snapshot.readable_id || refund.booking_id,currency(refund.amount),refund.payment_status,
      event?.status || 'Not queued',refund.processor_reference || 'No refund completed',openBookingButton(refund.booking_id)];
  }), state.simulation?'No refunds yet. Cancel a paid test booking, then send a test refund from its booking details.':'No external refunds recorded in this live workspace.');
  const filter = $('#activity-filter').value;
  dataTable($('#activity-directory'), ['Event', 'Booking', 'Status', 'Attempts', 'Last result', ''], state.events.filter(event => filter === 'all' || event.status === filter).map(event => {
    const booking = state.bookings.find(item => item.id === event.booking_id);
    const details = element('details');details.append(element('summary',event.last_error || (event.payload.status ? titles[event.payload.status] : 'View event')));
    details.append(element('pre',JSON.stringify({payload:event.payload, history:event.history},null,2)));
    return [event.kind,booking?.snapshot.readable_id || event.booking_id,event.status,event.attempts,details,openBookingButton(event.booking_id)];
  }), 'No matching sync events. Events appear when test bookings change.');
}

function showReadResult(target, data) {
  target.replaceChildren();
  const mode = data.mock || data.mode === 'mock' || state?.mode === 'mock' ? 'Mock response · synthetic data' : 'PMS response · live read';
  target.append(element('div', mode, 'result-caption'));
  const rows = Object.values(data).find(value => Array.isArray(value));
  if (rows?.length && rows.every(row => row && typeof row === 'object' && !Array.isArray(row))) {
    const keys = [...new Set(rows.flatMap(row => Object.keys(row)))].slice(0,12);
    const table = element('div');dataTable(table,keys,rows.map(row => keys.map(key => row[key])));target.append(table);
  }
  if (data.notice) target.append(element('p', data.notice, 'hint'));
  const raw = element('details', undefined, 'raw-response');raw.open = !rows?.length;
  raw.append(element('summary','Full API response'),element('pre',JSON.stringify(data,null,2)));target.append(raw);
}

async function runRead(button, target, path, body) {
  button.disabled = true;target.replaceChildren(element('p','Loading PMS response…','hint'));target.setAttribute('aria-busy','true');
  try { showReadResult(target,await api(path,body)); }
  catch (error) { target.replaceChildren(element('p',error.message,'notice')); }
  finally { button.disabled = false;target.setAttribute('aria-busy','false'); }
}

function buildQueryForm(operation) {
  const form = element('form',undefined,'query-form');form.id = operation + '-form';
  const controls = {};
  function add(name, text, control) { const label = element('label',text);control.name = name;control.required = true;label.append(control);form.append(label);controls[name] = control;return control; }
  const room = add('room_type_id','Mapped room type',element('select'));
  const audience = add('rate_audience','Rate audience',element('select'));
  for (const [value,text] of [['member','Member'],['friends_and_family','Friends & family']]) { const option = element('option',text);option.value=value;audience.append(option); }
  const packageSelect = add('package_id','Package / rate plan',element('select'));
  for (const [name,text,value] of [['arrival','Arrival · Dubai','2026-12-01'],['departure','Departure · Dubai','2026-12-03']]) { const input=element('input');input.type='date';input.value=value;add(name,text,input); }
  for (const row of mappingData?.mappings.room_types || []) { const option=element('option',`${row.name || row.wish_room_type_code} · ${row.wish_room_type_code}`);option.value=row.rufescent_room_type_id;room.append(option); }
  function packages() {
    packageSelect.replaceChildren();
    const row = mappingData?.mappings.room_types.find(item => item.rufescent_room_type_id === room.value);
    for (const [key,rate] of Object.entries(row?.rates || {})) {
      const [segment,packageId]=key.split(':');if(segment!==audience.value)continue;
      const option=element('option',`${packageId==='none'?'Room only':'Package '+packageId.slice(0,8)} · ${rate}`);option.value=packageId;packageSelect.append(option);
    }
  }
  room.onchange=packages;audience.onchange=packages;packages();
  const button=element('button',operation==='rates'?'Load rate plans':'Check availability','secondary');button.type='submit';button.disabled=!room.options.length;form.append(button);
  form.onsubmit=event=>{
    event.preventDefault();const values=Object.fromEntries(new FormData(form));
    const query={room_type_id:values.room_type_id,rate_audience:values.rate_audience,package_id:values.package_id==='none'?null:values.package_id,
      check_in_date:values.arrival+'T14:00:00+04:00',check_out_date:values.departure+'T12:00:00+04:00'};
    runRead(button,$('#'+operation+'-result'),'/api/rufescent/'+operation,query);
  };
  $('#'+operation+'-query').replaceChildren(form);
}

document.querySelectorAll('[data-tab]').forEach(button=>{
  button.onclick=()=>activateTab(button.dataset.tab);
  button.onkeydown=event=>{
    const tabs=[...document.querySelectorAll('[data-tab]')];let index=tabs.indexOf(button);
    if(event.key==='ArrowRight')index=(index+1)%tabs.length;
    else if(event.key==='ArrowLeft')index=(index-1+tabs.length)%tabs.length;
    else if(event.key==='Home')index=0;
    else if(event.key==='End')index=tabs.length-1;
    else return;
    event.preventDefault();activateTab(tabs[index].dataset.tab,true);
  };
});
document.querySelectorAll('[data-go]').forEach(button=>button.onclick=()=>activateTab(button.dataset.go));
window.addEventListener('hashchange',()=>activateTab(location.hash.slice(1)));
$('#property-setup').append(document.querySelector('.setup .panel'));
$('#property-search').oninput=renderTabs;$('#activity-filter').onchange=renderTabs;
$('#load-property').onclick=event=>runRead(event.currentTarget,$('#property-result'),'/api/masters/PROPERTY_INFO');
$('#load-rooms').onclick=event=>runRead(event.currentTarget,$('#rooms-result'),'/api/masters/ROOM_TYPE');
$('#pms-booking-form').onsubmit=event=>{event.preventDefault();const form=event.target;runRead(form.querySelector('button'),$('#pms-booking-result'),'/api/rufescent/pms-bookings/'+encodeURIComponent(form.elements.booking_id.value.trim()));};
$('#pms-search-form').onsubmit=event=>{event.preventDefault();const form=event.target;const data=Object.fromEntries(new FormData(form));data.date_from=data.date_from||null;data.date_to=data.date_to||null;runRead(form.querySelector('button'),$('#pms-search-result'),'/api/rufescent/pms-bookings/search',data);};
$('#live-booking-form').onsubmit=event=>{event.preventDefault();try{const payload=JSON.parse($('#live-booking-json').value);if(payload.sync_failure&&payload.sync_failure!=='none')throw new Error('Failure injection is unavailable in live mode.');action('/api/lifecycle/bookings',payload,'Live booking submitted. Check the PMS acknowledgement and activity.',payload.booking?.id);}catch(error){toast(error.message,true);}};
$('#guest-read-form').onsubmit=event=>{event.preventDefault();const form=event.target;runRead(form.querySelector('button'),$('#guest-result'),'/api/guests/'+encodeURIComponent(form.elements.guest_id.value.trim())+'/'+form.elements.operation.value);};
activateTab(location.hash.slice(1)||'properties');
(async()=>{
  const responses=await Promise.allSettled([api('/api/workspace/config'),api('/api/rufescent/mappings')]);
  if(responses[0].status==='fulfilled'){
    workspaceConfig=responses[0].value;
    const rows=[['Mode',workspaceConfig.mode],['Configured property',workspaceConfig.property_code],['Source ID',workspaceConfig.source_id],['JSON base URL',workspaceConfig.base_url],['JSON credentials',workspaceConfig.json_credentials_present?'Present (unverified)':'Missing'],['SOAP Pull URL',workspaceConfig.soap_pull_url||'Not configured'],['SOAP credentials',workspaceConfig.soap_credentials_present?'Present (unverified)':'Missing'],['Mapping data',workspaceConfig.synthetic_mappings?'Synthetic demo codes':'Configured codes (unverified)'],['Live lifecycle writes',workspaceConfig.live_writes_supported?'Some contracts configured; see capabilities below':'Blocked: vendor contracts/configuration missing']];
    dataTable($('#connection-summary'),['Setting','Value'],rows);
    const capabilities=element('div');dataTable(capabilities,['Write operation','Readiness','Missing requirements'],Object.entries(workspaceConfig.capabilities||{}).map(([name,cap])=>[name,cap.ready?'Configured (connectivity unverified)':'Blocked',cap.missing.join('; ')]));$('#connection-summary').append(capabilities);
    $('#property-form [name=pms_property_code]').value=workspaceConfig.property_code;
    $('#property-form [name=source_id]').value=workspaceConfig.source_id;
    const dot=element('i');document.querySelector('.mode').replaceChildren(dot,document.createTextNode(workspaceConfig.mode==='live'?'LIVE PMS MODE':'LOCAL SIMULATOR'));
  }else $('#connection-summary').append(element('p',responses[0].reason.message,'notice'));
  if(responses[1].status==='fulfilled'){
    mappingData=responses[1].value;
    if(mappingData.notice)$('#mapping-directory').append(element('p',mappingData.notice,'notice'));
    const holder=element('div');
    dataTable(holder,['Room type','Rufescent type ID','WISH room code','Audience : package','WISH rate plan'],mappingData.mappings.room_types.flatMap(row=>Object.entries(row.rates||{}).map(([key,value])=>[row.name,row.rufescent_room_type_id,row.wish_room_type_code,key,value])));
    $('#mapping-directory').append(holder);
    buildQueryForm('availability');buildQueryForm('rates');
  }else{
    for(const name of ['mapping-directory','availability-query','rates-query'])$('#'+name).append(element('p',responses[1].reason.message,'notice'));
  }
  renderTabs();
})();
