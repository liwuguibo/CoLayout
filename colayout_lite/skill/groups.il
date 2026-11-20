; Native figGroup context. Members are root DB figures, never child masters.
; IC618 skdfref/attrib: type=none is a plain editor group; figs is read-only.
procedure(ltGroupRefs(g)
  append(g~>figs append(list(g~>parent g~>figGroup)
    append(append(g~>children g~>groupMembers) append(g~>textDisplays g~>assocTextDisplays)))))
procedure(ltGroupsPrepare(cv seed @optional refOwners)
  let((n groups refUid)
    n=0
    groups=reverse(cv~>figGroups)
    unless(refOwners refOwners=makeTable('ltGroupRefOwnerSeen nil))
    foreach(g groups
      foreach(ref cons(g ltGroupRefs(g))
        when(ref && (ref~>cellView==cv || ref~>group~>cellView==cv || ref~>object~>cellView==cv) &&
          !ref~>colayoutObjectId && !ref~>colayoutRouteId
          refUid=ref~>colayoutReferenceId
          when(refUid && !refOwners[refUid] refOwners[refUid]=ref))))
    foreach(g groups
      foreach(ref cons(g ltGroupRefs(g))
        when(ref && (ref~>cellView==cv || ref~>group~>cellView==cv || ref~>object~>cellView==cv) &&
          !ref~>colayoutObjectId && !ref~>colayoutRouteId
          refUid=ref~>colayoutReferenceId
          when(!refUid || refOwners[refUid]!=ref
            refUid=nil
            while(!refUid || refOwners[refUid]
              n=n+1
              refUid=strcat(seed ":groupref:" sprintf(nil "%d" n)))
            dbReplaceProp(ref "colayoutReferenceId" "string" refUid)
            refOwners[refUid]=ref)))) t))
procedure(ltGroupAllowsDelete(g cv @optional (depth 0))
  !g || (depth<24 && g~>cellView==cv && g~>type=="none" && g~>colayoutReferenceId &&
    !g~>parent && !g~>children && !g~>groupMembers && !g~>textDisplays && !g~>assocTextDisplays &&
    ltGroupAllowsDelete(g~>figGroup cv depth+1)))
procedure(ltGroupsRead(cv)
  foreach(mapcar g cv~>figGroups
    list(ltRouteRef(g) g~>name g~>type lbP(g~>origin cv~>DBUPerUU) g~>orient g~>ordered lbProps(g)
      foreach(mapcar f g~>figs ltRouteRef(f))
      list(ltRouteRef(g~>parent) ltRouteRef(g~>figGroup)
        foreach(mapcar f g~>children ltRouteRef(f)) foreach(mapcar f g~>groupMembers ltRouteRef(f))
        foreach(mapcar f g~>textDisplays ltRouteRef(f)) foreach(mapcar f g~>assocTextDisplays ltRouteRef(f))))))
procedure(ltGroupsRequirePrepared(rows)
  let((seen)
    seen=makeTable('ltGroupIds nil)
    foreach(row rows
      when(seen[car(row)] error("DUPLICATE_FIGGROUP_ID")) seen[car(row)]=t
      unless(ltStableRouteRefs(list(car(row) nth(7 row) nth(8 row))) error("FIGGROUP_IDENTITY_NOT_PREPARED"))) t))
procedure(ltGroupExact(row deleted)
  let((members props)
    members=setof(uid nth(7 row) !deleted[uid])
    unless(nth(5 row) members=sort(members 'alphalessp))
    props=sort(copy(nth(6 row)) lambda((a b) alphalessp(car(a) car(b))))
    list(car(row) nth(1 row) nth(2 row) nth(3 row) nth(4 row) nth(5 row) props members nth(8 row))))
procedure(ltGroupsVerify(before cv deleted)
  let((after index empty)
    after=ltGroupsRead(cv) index=makeTable('ltGroupVerify nil) empty=makeTable('ltGroupDeletedNone nil)
    unless(length(before)==length(after) error("FIGGROUP_COLLECTION_CHANGED"))
    foreach(row after when(index[car(row)] error("DUPLICATE_FIGGROUP_ID")) index[car(row)]=row)
    foreach(row before
      unless(index[car(row)] && equal(ltGroupExact(row deleted) ltGroupExact(index[car(row)] empty))
        error("FIGGROUP_RELATIONSHIP_CHANGED"))) t))
