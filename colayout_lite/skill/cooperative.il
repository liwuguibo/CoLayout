; Cooperative non-fabricating operation-region guides inside native Virtuoso cellviews.
; Tagged shapes are hidden from lbExportCV/ltObjects/display reads and stripped from temporary clones.

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

procedure(ltSyncRegionGuides(lib cell guides)
  let((cv wasDirty tf scale boxLpp outlineLpp labelLpp rid rname labelText bb p1 p2 x0 y0 x1 y1 w h th margin cx cy fig)
    cv=ltWritable(lib cell)
    wasDirty=cv~>modifiedButNotSaved
    if(wasDirty && cv~>mode!="a" then
      list("skipped_dirty_readonly" 0 sprintf(nil "%L" cv~>modifiedCounter))
    else
      unless(ltEnsureEditable(cv) error("GUIDE_REOPEN_FAILED"))
      tf=techGetTechFile(cv)
      scale=cv~>DBUPerUU
      boxLpp=ltChooseGuideLpp(tf list(list("marker" "warning") list("y0" "drawing") list("annotate" "drawing") list("text" "drawing") list("prBoundary" "drawing")))
      outlineLpp=ltChooseGuideLpp(tf list(list("y0" "drawing") list("annotate" "drawing") list("marker" "warning") list("text" "drawing")))
      labelLpp=ltChooseGuideLpp(tf list(list("text" "drawing") list("annotate" "drawing") list("y0" "drawing") list("marker" "warning")))
      unless(boxLpp && labelLpp error("GUIDE_LAYER_UNAVAILABLE"))
      ltClearRegionGuides(cv)
      foreach(g guides
        rid=ltGet(g "id")
        rname=or(ltGet(g "name") "")
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
            fig=dbCreateRect(cv boxLpp list(list(x0 y0) list(x1 y1)))
            ltTagRegionGuide(fig rid rname)
            when(outlineLpp && !equal(outlineLpp boxLpp)
              fig=dbCreateRect(cv outlineLpp list(list(x0 y0) list(x1 y1)))
              ltTagRegionGuide(fig rid rname))
            w=1.0*(x1-x0)
            h=1.0*(y1-y0)
            th=min(1.2 max(0.08 min(h*0.14 w/max(1.0 strlen(labelText)*0.75))))
            margin=min(h*0.08 th*0.4)
            cx=(x0+x1)/2.0
            cy=y1-margin
            fig=dbCreateLabel(cv labelLpp list(cx cy) labelText "upperCenter" "R0" "stick" th)
            ltTagRegionGuide(fig rid rname))))
      unless(wasDirty unless(dbSave(cv) error("GUIDE_SAVE_FAILED")))
      ltRedrawCellView(cv)
      list(if(wasDirty then "drawn_unsaved" else "synced") length(guides) sprintf(nil "%L" cv~>modifiedCounter)))))
