(function installDiscoverCollaboration(root){
  "use strict";

  function inventoryRevision(refs){
    const inventory=[...refs].sort().join("\n");
    let hash=2166136261;
    for(let i=0;i<inventory.length;i++){
      hash^=inventory.charCodeAt(i);
      hash=Math.imul(hash,16777619);
    }
    return `v1:${refs.length}:${(hash>>>0).toString(36)}`;
  }

  function reviewState(row,revision){
    const value=row||{},raw=value.reviewed===true,current=raw&&value.reviewed_revision===revision;
    return {reviewed:current,stale:raw&&!current};
  }

  function reviewedPatch(editable,narrowed,checked,revision){
    if(!editable||narrowed) return null;
    return {reviewed:checked,reviewed_revision:checked?revision:null};
  }

  function categoryExportRecord({city,groupId,title,inventoryRevision,matijaRow,tundiRow}){
    const matija=matijaRow||{},tundi=tundiRow||{};
    if(![matija,tundi].some(row=>row.reviewed===true||(row.note||"").trim())) return null;
    const matijaState=reviewState(matija,inventoryRevision),tundiState=reviewState(tundi,inventoryRevision);
    return {city,groupId,title,inventoryRevision,
      matija:{reviewed:matijaState.reviewed,needsAnotherLook:matijaState.stale,note:matija.note||""},
      tundi:{reviewed:tundiState.reviewed,needsAnotherLook:tundiState.stale,note:tundi.note||""}};
  }

  function mergeAuthoritative(saved,pending){ return {...saved,...pending}; }

  function pendingFieldsAfterWatermark(mutationsByField,requestWatermark,current){
    const pending={};
    Object.entries(mutationsByField).forEach(([field,entries])=>{
      if((entries||[]).some(entry=>(entry?.seq??entry)>requestWatermark)) pending[field]=current[field];
    });
    return pending;
  }

  root.DISCOVER_COLLABORATION=Object.freeze({inventoryRevision,reviewState,reviewedPatch,categoryExportRecord,mergeAuthoritative,pendingFieldsAfterWatermark});
})(globalThis);
