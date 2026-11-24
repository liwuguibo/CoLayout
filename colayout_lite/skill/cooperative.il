; Cooperative non-fabricating operation-region guides inside native Virtuoso cellviews.
; Uses Virtuoso transient highlight sets (geCreateHilightSet) so region warning frames
; never mutate OA database geometry, increment modifiedCounter, trigger Layout XL extraction,
; or leave cv~>modifiedButNotSaved=t. Also keeps ltClearRegionGuides for cleaning any
; legacy persistent colayoutRegionGuide DB shapes on temporary/server-DRC copies.

unless(boundp('ltGuideHilightSets) ltGuideHilightSets=makeTable('ltGuideHilightSets nil))
unless(boundp('ltGuideSpecs) ltGuideSpecs=makeTable('ltGuideSpecs nil))

procedure(ltChooseGuideLpp(tf candidates)
  let((chosen)
    foreach(lpp candidates
      when(!chosen && techGetLP(tf lpp) chosen=lpp))
    unless(chosen
      foreach(lp tf~>lps
        when(!chosen && lp~>name && lp~>purpose
          chosen=list(lp~>name lp~>purpose))))
    chosen))

procedure(ltClearRegionGuides(cv)
  let((count)
    count=0
    foreach(s copy(cv~>shapes)
      when(dbIsId(s) && ltIsRegionGuide(s)
        when(dbDeleteObject(s) count=count+1)))
    count))

procedure(ltTagRegionGuide(fig rid rname)
  when(dbIsId(fig)
    dbReplaceProp(fig "colayoutRegionGuide" "boolean" t)
    dbReplaceProp(fig "colayoutRegionGuideId" "string" rid)
    when(stringp(rname) && rname!=""
      dbReplaceProp(fig "colayoutRegionName" "string" rname)))
  fig)

procedure(ltClearGuideHilights(key)
  let((sets ok)
    sets=ltGuideHilightSets[key]
    foreach(hs sets
      when(hs
        ok=errset(geIsValidHilightSet(hs) nil)
        when(ok && car(ok)
          errset(geDeleteHilightSet(hs) nil))))
    ltGuideHilightSets[key]=nil
    t))

procedure(ltApplyGuideHilights(cv key guides)
  let((tf scale boxLpp outlineLpp labelLpp boxHs outlineHs labelHs created rid labelText bb p1 p2 x0 y0 x1 y1 w h th margin cx cy)
    ltClearGuideHilights(key)
    when(guides
      tf=techGetTechFile(cv)
      scale=cv~>DBUPerUU
      boxLpp=ltChooseGuideLpp(tf list(list("marker" "warning") list("y0" "drawing") list("annotate" "drawing") list("prBoundary" "drawing")))
      outlineLpp=ltChooseGuideLpp(tf list(list("y0" "drawing") list("annotate" "drawing") list("marker" "warning")))
      labelLpp=ltChooseGuideLpp(tf list(list("y0" "drawing") list("annotate" "drawing") list("marker" "warning") list("text" "drawing")))
      unless(boxLpp && labelLpp error("GUIDE_LAYER_UNAVAILABLE"))
      boxHs=geCreateHilightSet(cv boxLpp nil)
      when(boxHs
        boxHs~>enable=t
        created=cons(boxHs created))
      when(outlineLpp && !equal(outlineLpp boxLpp)
        outlineHs=geCreateHilightSet(cv outlineLpp nil)
        when(outlineHs
          outlineHs~>enable=t
          created=cons(outlineHs created)))
      labelHs=cond(
        ((equal(labelLpp boxLpp) && boxHs) boxHs)
        ((equal(labelLpp outlineLpp) && outlineHs) outlineHs)
        (t let((hs)
          hs=geCreateHilightSet(cv labelLpp nil)
          when(hs
            hs~>enable=t
            created=cons(hs created))
          hs)))
      ltGuideHilightSets[key]=created
      foreach(g guides
        rid=ltGet(g "id")
        labelText=or(ltGet(g "label") "[CoLayout AI Region] DO NOT EDIT INSIDE")
        bb=ltGet(g "bbox")
        when(stringp(rid) && listp(bb) && length(bb)==2
          p1=ltPoint(car(bb) scale)
          p2=ltPoint(cadr(bb) scale)
          x0=min(car(p1) car(p2))
          y0=min(cadr(p1) cadr(p2))
          x1=max(car(p1) car(p2))
          y1=max(cadr(p1) cadr(p2))
          when(x1>x0 && y1>y0
            when(boxHs
              geAddHilightRectangle(boxHs list(list(x0 y0) list(x1 y1))))
            when(outlineHs
              geAddHilightRectangle(outlineHs list(list(x0 y0) list(x1 y1))))
            w=1.0*(x1-x0)
            h=1.0*(y1-y0)
            th=min(1.2 max(0.08 min(h*0.14 w/max(1.0 strlen(labelText)*0.75))))
            margin=min(h*0.08 th*0.4)
            cx=(x0+x1)/2.0
            cy=y1-margin
            when(labelHs
              geAddHilightLabel(labelHs list(cx cy) labelText "upperCenter" "R0" "stick" th t))))))
    ltRedrawCellView(cv)
    length(guides)))

procedure(ltSyncRegionGuides(lib cell guides @optional allowTemporary)
  let((key ddobj cv hasWin cleaned)
    if(!allowTemporary && rexMatchp("^COLAYOUT_MVP_LITE_" cell) then
      list("skipped_temporary" 0 "0")
    else
      key=strcat(lib "/" cell)
      ltGuideSpecs[key]=guides
      ddobj=ddGetObj(lib)
      cv=when(ddobj dbFindOpenCellView(ddobj cell "layout"))
      unless(cv cv=lbView(lib cell))
      when(exists(s cv~>shapes ltIsRegionGuide(s))
        when(!cv~>modifiedButNotSaved && ltEnsureEditable(cv)
          cleaned=ltClearRegionGuides(cv)
          when(cleaned>0
            ltRedrawCellView(cv)
            dbSave(cv)
            when(cv~>modifiedButNotSaved
              dbSave(cv)))))
      hasWin=exists(w hiGetWindowList() w!=hiGetCIWindow() && geGetWindowCellView(w)==cv)
      if(!hasWin then
        ltClearGuideHilights(key)
        list("cached_no_window" length(guides) sprintf(nil "%L" cv~>modifiedCounter))
      else
        ltApplyGuideHilights(cv key guides)
        list("synced" length(guides) sprintf(nil "%L" cv~>modifiedCounter)))))
