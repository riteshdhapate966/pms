'use strict';
const $ = selector => document.querySelector(selector);
let state, scenarios, selected, busy = false, toastTimer;
const refundKeys = new Map();
function readVendorFields(){if(state.simulation)return {};try{const value=JSON.parse($('#action-vendor-fields').value);if(!value||typeof value!=='object'||Array.isArray(value))throw new Error();return value;}catch{toast('Vendor fields must be a JSON object.',true);return null;}}
const titles = {pending:'Pending', confirmed:'Confirmed', checked_in:'Checked in', checked_out:'Checked out', cancelled:'Cancelled'};
const eventTitles = {'booking.created':'Booking created on PMS', 'booking.status_changed':'Booking status update', 'refund.completed':'Refund recorded on PMS'};
const currency = value => new Intl.NumberFormat('en-AE',{style:'currency',currency:'AED'}).format(Number(value));
function element(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; }
function toast(message, error=false) { clearTimeout(toastTimer); const box=$('#toast'); box.textContent=message; box.className='toast'+(error?' error':''); toastTimer=setTimeout(()=>box.classList.add('hidden'),7000); }
async function api(path, body) {
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok) throw new Error(Array.isArray(data.detail)?data.detail.map(item=>`${item.loc.join('.')}: ${item.msg}`).join('; '):(data.detail||'Request failed'));
  return data;
}
async function action(path, body, message, chooseId) {
  if(busy)return;
  busy=true; document.querySelectorAll('#property-form button,#booking-form button,#live-booking-form button,#booking-detail button,.booking-row,#refresh').forEach(button=>button.disabled=true);
  try {
    state=await api(path,body);
    if(chooseId)selected=chooseId;
    render(); toast(message);
  } catch(error) {toast(error.message,true);}
  finally {busy=false;render();}
}
function render() {
  if(!state)return;
  $('#live-warning').classList.toggle('hidden',state.simulation);
  if(!state.simulation)$('#live-warning').textContent='Live mode: reads call WISH directly. Writes require vendor-verified operation contracts. Missing operations are blocked; no simulation is used. Refund actions record an already-completed external refund, not a new payment refund.';
  $('#metric-properties').textContent=state.property_links.length;
  $('#metric-bookings').textContent=state.bookings.length;
  $('#metric-synced').textContent=state.bookings.filter(b=>b.sync_status==='synced').length;
  $('#metric-attention').textContent=state.bookings.filter(b=>b.sync_status!=='synced').length;
  const staySelect=$('#stay-select'), oldStay=staySelect.value;
  staySelect.replaceChildren(); $('#linked-properties').replaceChildren();
  for(const link of state.property_links) {
    const option=element('option',link.stay_name);option.value=link.stay_id;staySelect.append(option);
    $('#linked-properties').append(element('div',`${link.stay_name} → ${link.pms_property_code}`,'linked-item'));
  }
  if(!state.property_links.length){const option=element('option','Link a property first');option.value='';staySelect.append(option);}
  if([...staySelect.options].some(option=>option.value===oldStay))staySelect.value=oldStay;
  const list=$('#booking-list');list.replaceChildren();
  if(!selected && state.bookings.length)selected=state.bookings.at(-1).id;
  if(!state.bookings.length)list.append(element('div',state.simulation?'No bookings yet. Your first test stay will appear here.':'No live bookings tracked here yet. Search WISH above for existing reservations, or configure the create contract to create one.','empty'));
  for(const booking of [...state.bookings].reverse()) {
    const row=element('button',undefined,'booking-row'+(booking.id===selected?' active':''));row.type='button';row.dataset.bookingId=booking.id;
    const left=element('div'),right=element('div',undefined,'row-right');
    left.append(element('strong',booking.snapshot.primary_guest_name||booking.snapshot.guest_name));
    left.append(element('small',`${booking.snapshot.readable_id} · ${booking.snapshot.booking_rooms.length} rooms · ${booking.property_link.pms_property_code}`));
    right.append(element('span',titles[booking.status],'badge'+(booking.status==='cancelled'?' pending':'')));
    right.append(element('small',booking.sync_status==='synced'?'✓ PMS synced':'! PMS '+booking.sync_status));
    row.append(left,right);row.onclick=()=>{selected=booking.id;render();};list.append(row);
  }
  renderDetail();
  document.querySelectorAll('#property-form button').forEach(button=>button.disabled=busy||!state.property_links_enabled);
  document.querySelectorAll('#booking-form button,#booking-detail button').forEach(button=>button.disabled=busy||!state.writes_enabled||button.dataset.unavailable==='true');
  document.querySelectorAll('.booking-row,#refresh').forEach(button=>button.disabled=busy);
  $('#create-button').disabled=busy||!state.writes_enabled||!state.property_links.length;
  $('#live-create-button').disabled=busy||!state.capabilities?.create?.ready||!state.property_links.length;
  if(typeof renderTabs==='function')renderTabs();
}
function renderDetail() {
  const booking=state.bookings.find(b=>b.id===selected);if(!booking)return;
  const panel=$('#booking-detail');panel.replaceChildren($('#detail-template').content.cloneNode(true));
  const field=name=>panel.querySelector(`[data-field="${name}"]`);
  const set=(name,text)=>field(name).textContent=text;
  $('#failure-control').classList.toggle('hidden',!state.simulation);
  $('#vendor-fields-control').classList.toggle('hidden',state.simulation);
  if(!state.simulation){panel.querySelector('.compare>div:first-child .eyebrow').textContent='LOCAL STAY RECORD';panel.querySelector('.compare>div:last-child .eyebrow').textContent='PMS · LAST ACKNOWLEDGEMENT';field('paid').previousElementSibling.textContent='Reported amount paid';field('refunded').previousElementSibling.textContent='External refund recorded';}
  set('readable_id',booking.snapshot.readable_id);
  set('guest_name',booking.snapshot.primary_guest_name||booking.snapshot.guest_name);
  set('sync_badge',booking.sync_status==='synced'?'✓ Synced':'Needs attention');field('sync_badge').classList.add(booking.sync_status);
  const date=value=>new Date(value).toLocaleDateString('en-GB',{timeZone:'Asia/Dubai',day:'numeric',month:'short',year:'numeric'});
  set('meta',`${date(booking.snapshot.check_in_date)} — ${date(booking.snapshot.check_out_date)} · ${booking.property_link.stay_name} · ${booking.snapshot.rate_audience==='member'?'Member':'Friends & family'}`);
  set('local_status',titles[booking.status]);set('local_id',booking.id);
  set('pms_status',booking.pms?titles[booking.pms.status]:'Awaiting creation');
  set('pms_id',booking.pms?`${booking.pms.id} · ${booking.pms.property_code}`:'No PMS reservation ID yet');
  const events=state.events.filter(event=>event.booking_id===booking.id);
  const failed=events.find(event=>['failed','reconciliation_required','in_flight'].includes(event.status));
  if(failed){field('sync_error').classList.remove('hidden');set('sync_error',`${failed.last_error}. Local changes are saved. ${['reconciliation_required','in_flight'].includes(failed.status)?'Do not replay this operation: verify its actual PMS outcome and reconcile it first.':'Retry PMS sync after resolving the reported error.'}`);}
  const actions=field('status_actions');
  for(const target of state.transitions[booking.status]||[]) {
    const button=element('button',({confirmed:'Confirm booking',checked_in:'Check in',checked_out:'Check out',cancelled:'Cancel booking'})[target],target==='cancelled'?'cancel':'');button.type='button';
    if(!state.simulation&&!state.capabilities[target]?.ready){button.dataset.unavailable='true';button.title=state.capabilities[target]?.missing.join('; ')||'Vendor operation unavailable';}
    button.onclick=()=>{const fields=readVendorFields();if(fields===null)return;action(`/api/lifecycle/bookings/${booking.id}/status`,{status:target,sync_failure:$('#action-failure').value,vendor_fields:fields},'Stay status saved. Check the PMS sync result below.');};actions.append(button);
  }
  if(booking.sync_status!=='synced') {
    const uncertain=events.some(event=>['reconciliation_required','in_flight'].includes(event.status));const button=element('button',uncertain?'Reconciliation required':'↻ Retry PMS sync','retry');button.type='button';if(uncertain)button.dataset.unavailable='true';button.onclick=()=>action(`/api/lifecycle/bookings/${booking.id}/retry-sync`,{},'PMS sync retry completed.');actions.append(button);
  }
  set('total',currency(booking.snapshot.total_amount));set('paid',currency(booking.paid_amount));set('refunded',currency(booking.refunded_amount));set('pms_refunded',booking.pms?currency(booking.pms.refunded_amount):'—');
  const refunds=state.refunds.filter(refund=>refund.booking_id===booking.id);
  const failedRefund=refunds.find(refund=>refund.payment_status==='failed');
  if(booking.status==='cancelled'&&Number(booking.refundable_amount)>0) {
    const box=element('div',undefined,'refund-box');box.append(element('h3',state.simulation?(failedRefund?'Retry simulated refund':'Refund this cancelled stay'):'Record an externally completed refund'));
    box.append(element('p',`Refundable: ${currency(booking.refundable_amount)}. A refund is a separate action; cancellation alone does not return money. PMS sync retries never issue another refund.`));
    const form=element('form');form.id='refund-form';
    const amountLabel=element('label','Amount · AED'), amount=element('input');amount.name='amount';amount.type='number';amount.step='0.01';amount.min='0.01';amount.max=booking.refundable_amount;amount.value=failedRefund?failedRefund.amount:booking.refundable_amount;amount.required=true;amount.readOnly=!!failedRefund;amountLabel.append(amount);
    const modeLabel=element('label','Payment test'), mode=element('select');mode.name='payment_failure';for(const [value,text] of [['no','Refund succeeds'],['yes','Refund processor fails']]){const option=element('option',text);option.value=value;mode.append(option);}modeLabel.append(mode);
    const button=element('button',state.simulation?(failedRefund?'Retry refund':'Send test refund'):'Record refund on PMS','primary');button.type='submit';form.append(amountLabel);
    let processorReference=null;
    if(state.simulation)form.append(modeLabel);else{const label=element('label','External refund transaction reference');processorReference=element('input');processorReference.required=true;processorReference.maxLength=255;label.append(processorReference);form.append(label);if(!state.capabilities.refund_record?.ready){button.dataset.unavailable='true';button.title=state.capabilities.refund_record?.missing.join('; ')||'Refund contract unavailable';}}
    form.append(button);
    if(!refundKeys.has(booking.id))refundKeys.set(booking.id,crypto.randomUUID());
    form.onsubmit=async event=>{event.preventDefault();const fields=readVendorFields();if(fields===null)return;const key=failedRefund?failedRefund.idempotency_key:(refundKeys.get(booking.id)||crypto.randomUUID());refundKeys.set(booking.id,key);await action(`/api/lifecycle/bookings/${booking.id}/refunds`,{idempotency_key:key,amount:amount.value,simulate_payment_failure:state.simulation&&mode.value==='yes',sync_failure:$('#action-failure').value,processor_reference:processorReference?.value||null,vendor_fields:fields},'Refund record processed. Review payment and PMS results separately.');if(state.refunds.some(refund=>refund.idempotency_key===key&&refund.payment_status==='completed'))refundKeys.delete(booking.id);};
    box.append(form);field('refund_area').append(box);
  } else if(booking.status==='cancelled'&&Number(booking.paid_amount)===0){field('refund_area').append(element('p','No refund is due: no simulated payment was recorded.','hint'));}
  for(const refund of refunds)field('refund_area').append(element('div',`${refund.payment_status==='completed'?'✓ Refund completed':'! Refund failed'} · ${currency(refund.amount)} · ${refund.processor_reference||'No money returned'} · ${refund.attempts} payment attempt(s)`,'refund-record'));
  for(const event of events) {
    const row=element('div',undefined,'event'),dot=element('span',undefined,'event-dot '+event.status),body=element('div');
    body.append(element('strong',(eventTitles[event.kind]||event.kind)+(event.payload.status?' · '+titles[event.payload.status]:'')));
    body.append(element('small',`${new Date(event.created_at).toLocaleTimeString()} · ${event.attempts} attempt(s)${event.last_error?' · '+event.last_error:''}`));
    if(event.history.length>1)body.append(element('small',event.history.map(attempt=>`#${attempt.attempt} ${attempt.result}`).join(' → ')));
    row.append(dot,body,element('span',event.status,'badge '+event.status));field('timeline').append(row);
  }
}
$('#property-form').onsubmit=event=>{event.preventDefault();action('/api/lifecycle/property-links',Object.fromEntries(new FormData(event.target)),'Stay linked to the PMS property.');};
$('#scenario-select').onchange=()=>{$('#booking-form [name=guest_name]').value=$('#scenario-select').value==='member_multi_room'?'Demo Booking Member':'Demo Staying Guest';};
$('#booking-form').onsubmit=event=>{
  event.preventDefault();const form=Object.fromEntries(new FormData(event.target));
  const booking=structuredClone(scenarios[form.scenario].booking);booking.id=crypto.randomUUID();booking.readable_id='TEST-'+booking.id.slice(0,8).toUpperCase();booking.status=form.status;booking.booking_rooms.forEach(room=>room.id=crypto.randomUUID());
  booking.check_in_date=form.arrival+'T14:00:00+04:00';booking.check_out_date=form.departure+'T12:00:00+04:00';
  if(booking.primary_guest_is_member)booking.guest_name=form.guest_name;else booking.primary_guest_name=form.guest_name;
  action('/api/lifecycle/bookings',{stay_id:form.stay_id,booking,paid_amount:form.paid_amount,sync_failure:form.sync_failure},'Booking saved. Review the local and PMS statuses.',booking.id);
};
$('#refresh').onclick=async()=>{try{state=await api('/api/lifecycle/state');render();}catch(error){toast(error.message,true);}};
(async()=>{try{state=await api('/api/lifecycle/state');scenarios=state.simulation?await api('/api/rufescent/scenarios'):null;render();}catch(error){$('#booking-list').textContent='Unable to load workspace.';toast(error.message,true);}})();
