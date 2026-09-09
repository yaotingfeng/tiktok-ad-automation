SELECT s.id 
FROM execution_step s JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.id=s.unit_id
JOIN submission_unit su ON su.tenant_id=s.tenant_id AND su.submission_id=s.submission_id AND su.unit_id=s.unit_id
WHERE s.tenant_id=:tenant AND s.submission_id=:submission AND su.expanded AND su.disposition='INCLUDED'
AND s.dispatch_id IS NULL AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= :now)

AND s.status IN ('FAILED','RETRYABLE') AND s.request_body IS NULL AND s.remote_id IS NULL
AND s.phase<>'REQUEST_ARMED' AND s.kind<>'READBACK' AND coalesce(s.error_code,'') NOT IN ('scene_intent_changed','scene_no_longer_supported','new_preview_required','copy_too_long','blank_copy','invalid_copy')
AND NOT EXISTS (SELECT 1 FROM step_evidence e WHERE e.tenant_id=s.tenant_id AND e.submission_id=s.submission_id AND e.step_id=s.id
 AND (e.conclusion IN ('REQUEST_ARMED','CREATED','LATE_CREATED') OR e.summary ? 'remote_id'))
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND s.distribution_id IS NULL) OR EXISTS (SELECT 1 FROM material_cover_job c WHERE c.id=s.cover_job_id AND c.tenant_id=s.tenant_id AND c.bc_id=s.bc_id
AND c.material_id=s.material_id AND c.advertiser_id=u.advertiser_id AND c.connection_id=u.connection_id
AND c.status='BLOCKED' AND c.request_armed_at IS NULL AND c.known_image_id IS NULL
AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
AND NOT EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id)))
AND (s.parent_step_id IS NULL OR EXISTS (SELECT 1 FROM execution_step p WHERE p.tenant_id=s.tenant_id AND p.submission_id=s.submission_id AND p.id=s.parent_step_id AND p.status='SUCCEEDED' AND p.remote_id IS NOT NULL))
AND (s.kind<>'CAMPAIGN' OR EXISTS (SELECT 1 FROM execution_step cta WHERE cta.tenant_id=s.tenant_id AND cta.submission_id=s.submission_id AND cta.unit_id=s.unit_id AND cta.kind='CTA' AND cta.status='SUCCEEDED' AND cta.remote_id IS NOT NULL))
AND (s.kind NOT IN ('CAMPAIGN','ADGROUP') OR EXISTS (SELECT 1 FROM planned_group g
 WHERE g.tenant_id=s.tenant_id AND g.preview_id=s.preview_id AND g.unit_id=s.unit_id
 AND (s.kind='CAMPAIGN' OR g.id=s.group_id)
 AND EXISTS (SELECT 1 FROM preview_group_material gm WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no)
 AND NOT EXISTS (SELECT 1 FROM preview_group_material gm
 WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM execution_step ms WHERE ms.tenant_id=s.tenant_id AND ms.submission_id=s.submission_id AND ms.unit_id=s.unit_id AND ms.kind='MATERIAL' AND ms.material_id=gm.material_id AND ms.status='SUCCEEDED')))
)

AND u.connection_id=(SELECT a.connection_id FROM bc_account_access a
 JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
 JOIN tenant_bc b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id
 JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
 WHERE a.tenant_id=s.tenant_id AND a.bc_id=s.bc_id AND a.advertiser_id=u.advertiser_id
 AND a.in_bc AND a.authorized AND a.active AND a.can_build AND a.permission_state='VERIFIED'
 AND c.status='ACTIVE' AND NOT b.ownership_conflict AND NOT aa.ownership_conflict
 AND aa.remote_status IN ('ENABLE','STATUS_ENABLE') AND trim(aa.currency)<>'' AND trim(aa.timezone)<>''
 AND aa.currency=u.currency AND aa.timezone=u.timezone ORDER BY a.connection_id LIMIT 1)
