SELECT s.id 
FROM execution_step s JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.id=s.unit_id
JOIN submission_unit su ON su.tenant_id=s.tenant_id AND su.submission_id=s.submission_id AND su.unit_id=s.unit_id
WHERE s.tenant_id=:tenant AND s.submission_id=:submission AND su.expanded AND su.disposition='INCLUDED'
AND s.dispatch_id IS NULL AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= :now)

AND (s.status='UNKNOWN' OR s.mismatch OR (s.kind='MATERIAL' AND s.status='FAILED') OR (s.kind='READBACK' AND s.status<>'SUCCEEDED' AND EXISTS
 (SELECT 1 FROM execution_step p WHERE p.tenant_id=s.tenant_id AND p.submission_id=s.submission_id AND p.id=s.parent_step_id AND p.remote_id IS NOT NULL)))
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND EXISTS (SELECT 1 FROM material_distribution d JOIN material_asset_operation o
 ON o.id=d.operation_id AND o.tenant_id=d.tenant_id AND o.bc_id=d.bc_id AND o.material_id=d.material_id AND o.advertiser_id=d.advertiser_id
 WHERE d.id=s.distribution_id AND d.tenant_id=s.tenant_id AND d.bc_id=s.bc_id AND d.material_id=s.material_id AND d.advertiser_id=u.advertiser_id
 AND d.status IN ('result_unknown','verifying','preparing','ready','blocked') AND o.status IN ('sending','result_unknown','verifying','succeeded')))
 OR EXISTS (SELECT 1 FROM material_cover_job c WHERE c.id=s.cover_job_id AND c.tenant_id=s.tenant_id AND c.bc_id=s.bc_id
AND c.material_id=s.material_id AND c.advertiser_id=u.advertiser_id AND c.connection_id=u.connection_id
 AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
 AND (c.request_armed_at IS NOT NULL OR c.known_image_id IS NOT NULL
 OR EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id))))

AND u.connection_id=(SELECT a.connection_id FROM bc_account_access a
 JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
 JOIN tenant_bc b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id
 JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
 WHERE a.tenant_id=s.tenant_id AND a.bc_id=s.bc_id AND a.advertiser_id=u.advertiser_id
 AND a.in_bc AND a.authorized AND a.active AND a.can_build AND a.permission_state='VERIFIED'
 AND c.status='ACTIVE' AND NOT b.ownership_conflict AND NOT aa.ownership_conflict
 AND aa.remote_status IN ('ENABLE','STATUS_ENABLE') AND trim(aa.currency)<>'' AND trim(aa.timezone)<>''
 AND aa.currency=u.currency AND aa.timezone=u.timezone ORDER BY a.connection_id LIMIT 1)
