import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source=readFileSync(new URL("./discover-collaboration.js",import.meta.url),"utf8");
const pageSource=readFileSync(new URL("./index.html",import.meta.url),"utf8");
const sandbox=Object.create(null);
vm.runInNewContext(source,sandbox,{filename:"discover-collaboration.js"});
const api=sandbox.DISCOVER_COLLABORATION;

function extractFunction(name){
  const start=pageSource.indexOf(`function ${name}(`);
  assert.notEqual(start,-1,`${name} must exist in index.html`);
  const bodyStart=pageSource.indexOf("{",start);
  let depth=0;
  for(let index=bodyStart;index<pageSource.length;index++){
    if(pageSource[index]==="{") depth++;
    else if(pageSource[index]==="}"&&--depth===0) return pageSource.slice(start,index+1);
  }
  throw new Error(`Could not extract ${name}`);
}

test("narrowed category views cannot be marked reviewed",()=>{
  assert.equal(api.reviewedPatch(true,true,true,"v1:1:abc"),null);
  assert.equal(api.reviewedPatch(false,false,true,"v1:1:abc"),null);
  assert.deepEqual(
    {...api.reviewedPatch(true,false,true,"v1:1:abc")},
    {reviewed:true,reviewed_revision:"v1:1:abc"},
  );
});

test("inventory membership changes make a previous review stale",()=>{
  const before=api.inventoryRevision(["budapest|a","budapest|b"]);
  const after=api.inventoryRevision(["budapest|a","budapest|b","budapest|c"]);
  assert.notEqual(after,before);
  assert.deepEqual({...api.reviewState({reviewed:true,reviewed_revision:before},before)},{reviewed:true,stale:false});
  assert.deepEqual({...api.reviewState({reviewed:true,reviewed_revision:before},after)},{reviewed:false,stale:true});
});

test("category-only notes are present in schema-v3 export records",()=>{
  const revision=api.inventoryRevision(["budapest|market"]);
  const record=api.categoryExportRecord({
    city:"budapest",groupId:"markets",title:"Markets & food halls",inventoryRevision:revision,
    matijaRow:{note:"Maybe pair it with lunch",reviewed:false,reviewed_revision:null},tundiRow:{},
  });
  const document={schemaVersion:3,activities:[],categories:[record].filter(Boolean)};
  assert.equal(document.activities.length,0);
  assert.equal(document.categories.length,1);
  assert.equal(document.categories[0].matija.note,"Maybe pair it with lunch");
});

test("authoritative saves keep remote fields unless that field is locally pending",()=>{
  const saved={note:"newer remote note",reviewed:true,reviewed_revision:"v1:2:new"};
  const current={note:"stale local note",reviewed:false,reviewed_revision:null};
  assert.deepEqual(
    {...api.mergeAuthoritative(saved,{reviewed:current.reviewed,reviewed_revision:current.reviewed_revision})},
    {note:"newer remote note",reviewed:false,reviewed_revision:null},
  );
});

test("only edits newer than a request watermark override its authoritative response",()=>{
  const current={note:"typed after request",reviewed:false};
  assert.deepEqual(
    {...api.pendingFieldsAfterWatermark({note:[{seq:4}],reviewed:[{seq:3}]},4,current)},
    {},
    "a completed mutation still awaiting realtime echo is not pending",
  );
  assert.deepEqual(
    {...api.pendingFieldsAfterWatermark({note:[{seq:4},{seq:5}],reviewed:[{seq:3}]},4,current)},
    {note:"typed after request"},
    "a genuinely later edit remains optimistic",
  );
});

test("optimistic mutations survive requests delayed beyond eight seconds until an actual settlement",()=>{
  const timers=[];
  const state=Object.assign(Object.create(null),{
    Map,
    setTimeout:(callback,delay)=>{timers.push({callback,delay});return timers.length;},
  });
  vm.runInNewContext(`
    let mutationSeq=0;
    const localMutations=new Map(),latestMutationSeqs=new Map();
    const UI_FIELDS=["note"];
    const mutationKey=(userId,pk,field)=>JSON.stringify([userId,pk,field]);
    ${extractFunction("removeLocalMutation")}
    ${extractFunction("recordLocalMutation")}
    ${extractFunction("settleLocalMutations")}
    ${extractFunction("consumeLocalEcho")}
    globalThis.testApi={
      recordLocalMutation,settleLocalMutations,consumeLocalEcho,
      pending:(userId,pk,field)=>(localMutations.get(mutationKey(userId,pk,field))||[]).length,
      latest:(userId,pk,field)=>latestMutationSeqs.get(mutationKey(userId,pk,field))||0,
    };
  `,state,{filename:"index-mutation-ledger.js"});

  const mutation=state.testApi.recordLocalMutation("user-1","budapest|place","note","still typing");
  assert.equal(state.testApi.pending("user-1","budapest|place","note"),1);
  timers.filter(timer=>timer.delay<=9000).forEach(timer=>timer.callback());
  assert.equal(timers.length,0,"mutation lifetime must not depend on a wall-clock timeout");
  assert.equal(state.testApi.pending("user-1","budapest|place","note"),1,"a 9-second request remains optimistic");

  state.testApi.settleLocalMutations([mutation]);
  assert.equal(state.testApi.pending("user-1","budapest|place","note"),0);
  assert.equal(state.testApi.latest("user-1","budapest|place","note"),0);

  const first=state.testApi.recordLocalMutation("user-1","budapest|other","note","first");
  const second=state.testApi.recordLocalMutation("user-1","budapest|other","note","second");
  state.testApi.settleLocalMutations([first]);
  assert.equal(state.testApi.pending("user-1","budapest|other","note"),2,"an older request stays as an echo guard while a newer edit is pending");
  const preserved=state.testApi.consumeLocalEcho(
    {user_id:"user-1",place_key:"budapest|other",note:"first"},
    {note:"second"},
  );
  assert.equal(preserved.note,"second","the late first realtime echo cannot clobber the second optimistic edit");
  assert.equal(state.testApi.pending("user-1","budapest|other","note"),1);
  state.testApi.settleLocalMutations([second]);
  assert.equal(state.testApi.pending("user-1","budapest|other","note"),0);
  assert.match(pageSource,/settleLocalMutations\(mutations\);/,"successful Supabase responses must settle their mutation records");
});

test("a realtime echo cannot overwrite a newer pending field in the same row",()=>{
  const state=Object.assign(Object.create(null),{Map});
  vm.runInNewContext(`
    let mutationSeq=0;
    const localMutations=new Map(),latestMutationSeqs=new Map();
    const UI_FIELDS=["note","reviewed"];
    const mutationKey=(userId,pk,field)=>JSON.stringify([userId,pk,field]);
    ${extractFunction("removeLocalMutation")}
    ${extractFunction("recordLocalMutation")}
    ${extractFunction("consumeLocalEcho")}
    globalThis.testApi={recordLocalMutation,consumeLocalEcho,
      pending:(userId,pk,field)=>(localMutations.get(mutationKey(userId,pk,field))||[]).length};
  `,state,{filename:"index-cross-field-echo.js"});

  const userId="user-1",pk="@discover-group:v1|budapest|markets";
  state.testApi.recordLocalMutation(userId,pk,"note","local note");
  state.testApi.recordLocalMutation(userId,pk,"reviewed",true);
  const preserved=state.testApi.consumeLocalEcho(
    {user_id:userId,place_key:pk,note:"local note",reviewed:false},
    {note:"local note",reviewed:true},
  );
  assert.equal(preserved.reviewed,true,"the note echo must preserve the newer review check");
  assert.equal(state.testApi.pending(userId,pk,"note"),0);
  assert.equal(state.testApi.pending(userId,pk,"reviewed"),1);
});

test("focused notes are preserved only while an edit is dirty or in flight",()=>{
  const state=Object.assign(Object.create(null),{Map});
  vm.runInNewContext(`
    const authGeneration=7,noteTimers=new Map(),localMutations=new Map();
    const noteTimerKey=(pk,person)=>JSON.stringify([pk,person]);
    const mutationKey=(userId,pk,field)=>JSON.stringify([userId,pk,field]);
    const targetUserId=person=>person==="tundi"?"user-tundi":null;
    ${extractFunction("hasPendingNote")}
    globalThis.testApi={hasPendingNote,
      setTimer:(pk,person,generation)=>noteTimers.set(noteTimerKey(pk,person),{generation}),
      clearTimer:(pk,person)=>noteTimers.delete(noteTimerKey(pk,person)),
      setMutation:(pk,person)=>localMutations.set(mutationKey(targetUserId(person),pk,"note"),[{seq:1}]),
    };
  `,state,{filename:"index-focused-note-state.js"});

  const pk="@discover-group:v1|budapest|markets";
  assert.equal(state.testApi.hasPendingNote(pk,"tundi"),false,"focus alone is not a local edit");
  state.testApi.setTimer(pk,"tundi",7);
  assert.equal(state.testApi.hasPendingNote(pk,"tundi"),true);
  state.testApi.clearTimer(pk,"tundi");
  state.testApi.setMutation(pk,"tundi");
  assert.equal(state.testApi.hasPendingNote(pk,"tundi"),true);
  assert.match(pageSource,/preserveValue:hasPendingNote\(pk,activeLane\.dataset\.person\)/);
  assert.match(pageSource,/preserveDirtyNote=.*hasPendingNote\(pk,p\.key\)/);
});

test("consecutive failed saves restore the last confirmed row instead of an earlier failed edit",()=>{
  const state=Object.assign(Object.create(null),{Map});
  vm.runInNewContext(`
    let mutationSeq=0;
    const localMutations=new Map(),latestMutationSeqs=new Map(),confirmedRows=new Map();
    const UI_FIELD_DEFAULTS={rating:null,note:null,keep:null,reviewed:false,reviewed_revision:null};
    const mutationKey=(userId,pk,field)=>JSON.stringify([userId,pk,field]);
    const laneKey=(userId,pk)=>JSON.stringify([userId,pk]);
    ${extractFunction("removeLocalMutation")}
    ${extractFunction("recordLocalMutation")}
    ${extractFunction("rollbackFailedMutations")}
    globalThis.testApi={
      setBaseline:(userId,pk,row)=>confirmedRows.set(laneKey(userId,pk),{...row}),
      recordPatch:(userId,pk,patch)=>Object.entries(patch).map(([field,value])=>recordLocalMutation(userId,pk,field,value)),
      fail:(userId,pk,row,mutations)=>rollbackFailedMutations(userId,pk,row,mutations),
      pending:()=>[...localMutations.values()].reduce((count,entries)=>count+entries.length,0),
    };
  `,state,{filename:"index-save-failure-baseline.js"});

  const userId="user-1",pk="@discover-group:v1|budapest|markets";
  const baseline={note:"server-confirmed",reviewed:true,reviewed_revision:"rev-server"};
  const firstPatch={note:"failed A",reviewed:false,reviewed_revision:null};
  const secondPatch={note:"failed B",reviewed:true,reviewed_revision:"rev-new"};
  state.testApi.setBaseline(userId,pk,baseline);
  const first=state.testApi.recordPatch(userId,pk,firstPatch);
  const second=state.testApi.recordPatch(userId,pk,secondPatch);
  const active={...secondPatch};

  state.testApi.fail(userId,pk,active,first);
  assert.deepEqual(active,secondPatch,"failure A must preserve the newer optimistic B values");
  assert.equal(state.testApi.pending(),3);

  state.testApi.fail(userId,pk,active,second);
  assert.deepEqual(active,baseline,"failure B must restore the last server-confirmed note and review pair");
  assert.equal(state.testApi.pending(),0);
  assert.match(pageSource,/rollbackFailedMutations\(userId,pk,activeRow,mutations\)/);
});

test("realtime changes are buffered during the cloud snapshot and replayed in order",()=>{
  const state=Object.assign(Object.create(null),{applied:[]});
  vm.runInNewContext(`
    let me={id:"user-1"},cloudReady=false;
    const pendingRealtimeEvents=[];
    function applyRealtimeChange(payload,refresh=true){ applied.push([payload.id,refresh]); }
    ${extractFunction("handleRealtimeChange")}
    ${extractFunction("replayBufferedRealtimeChanges")}
    globalThis.testApi={
      handleRealtimeChange,replayBufferedRealtimeChanges,
      ready:()=>{cloudReady=true;},
      buffered:()=>pendingRealtimeEvents.length,
    };
  `,state,{filename:"index-realtime-buffer.js"});

  state.testApi.handleRealtimeChange({id:"first"});
  state.testApi.handleRealtimeChange({id:"second"});
  assert.equal(state.testApi.buffered(),2);
  assert.equal(state.applied.length,0);
  state.testApi.ready();
  state.testApi.replayBufferedRealtimeChanges();
  state.testApi.handleRealtimeChange({id:"third"});
  assert.deepEqual(JSON.parse(JSON.stringify(state.applied)),[["first",false],["second",false],["third",true]]);
  assert.match(pageSource,/snapshot\.rows\.forEach\(indexRow\); cloudReady=true; syncError="";\s*replayBufferedRealtimeChanges\(\);/);
});

test("older realtime rows cannot overwrite a newer authoritative response",()=>{
  const state=Object.create(null);
  vm.runInNewContext(`
    ${extractFunction("isStaleRealtimeRow")}
    globalThis.testApi={isStaleRealtimeRow};
  `,state,{filename:"index-realtime-order.js"});
  assert.equal(state.testApi.isStaleRealtimeRow(
    {updated_at:"2026-07-16T20:00:00.100000+00:00"},
    {updated_at:"2026-07-16T20:00:00.200000+00:00"},
  ),true);
  assert.equal(state.testApi.isStaleRealtimeRow(
    {updated_at:"2026-07-16T20:00:00.300000+00:00"},
    {updated_at:"2026-07-16T20:00:00.200000+00:00"},
  ),false);
  assert.match(pageSource,/if\(isStaleRealtimeRow\(row,current\)\) return;/);
});

test("hidden category notes are autosized on the animation frame after their group opens",()=>{
  const state=Object.assign(Object.create(null),{frames:[],sized:[]});
  vm.runInNewContext(`
    function requestAnimationFrame(callback){ frames.push(callback); }
    function autosize(note){ sized.push(note.id); }
    ${extractFunction("autosizeVisibleGroupNotes")}
    const notes=[{id:"matija"},{id:"tundi"}];
    const el={open:false,querySelectorAll:selector=>selector===".group-note"?notes:[]};
    globalThis.testApi={
      schedule:()=>autosizeVisibleGroupNotes(el),
      open:()=>{el.open=true;},
      close:()=>{el.open=false;},
      flush:()=>frames.shift()?.(),
    };
  `,state,{filename:"index-group-autosize.js"});

  state.testApi.schedule();
  assert.equal(state.frames.length,0,"closed details must not measure hidden textareas");
  state.testApi.open();
  state.testApi.schedule();
  assert.equal(state.sized.length,0,"measurement waits until layout is visible");
  assert.equal(state.frames.length,1);
  state.testApi.flush();
  assert.deepEqual(JSON.parse(JSON.stringify(state.sized)),["matija","tundi"]);
  assert.match(pageSource,/if\(el\.open\)\{ openGroups\.add\(group\.id\); mountGroup\(el,items\); autosizeVisibleGroupNotes\(el\); \}/);
});

test("category note labels include the category and note-only patches skip full stats scans",()=>{
  assert.match(pageSource,/category note — \$\{title\}/);
  assert.match(pageSource,/const dataByKey=new Map\(DATA\.map/);
  assert.match(pageSource,/const nowRelevant=hasRelevantRating\(pk\)/);
  assert.match(pageSource,/if\(wasRelevant!==nowRelevant\) updateStats\(\)/);
});
