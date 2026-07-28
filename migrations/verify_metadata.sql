-- ============================================================
-- Verification Queries — รันหลัง migration และ sync ไฟล์แล้ว
-- ============================================================

-- 1. ดู chunks ล่าสุด พร้อม metadata ครบ
SELECT
  id,
  file_id,
  LEFT(content, 100)              AS content_preview,
  is_active,
  chunk_index,
  page_number,
  section_title,
  version,
  metadata->>'file_name'          AS file_name,
  metadata->>'file_type'          AS file_type,
  metadata->>'category'           AS category,
  metadata->>'language'           AS language,
  metadata->>'chunk_id'           AS chunk_id,
  (metadata->>'is_active')::bool  AS meta_is_active,
  metadata
FROM knowledge_chunks
ORDER BY created_at DESC
LIMIT 20;


-- 2. ตรวจสอบ metadata ว่ามี null หรือ empty
SELECT
  COUNT(*)                                          AS total_chunks,
  COUNT(*) FILTER (WHERE is_active = true)          AS active_chunks,
  COUNT(*) FILTER (WHERE is_active = false)         AS inactive_chunks,
  COUNT(*) FILTER (WHERE metadata = '{}'::jsonb)    AS empty_metadata,
  COUNT(*) FILTER (WHERE metadata IS NULL)          AS null_metadata,
  COUNT(*) FILTER (WHERE chunk_index IS NULL)       AS missing_chunk_index,
  COUNT(*) FILTER (WHERE page_number IS NOT NULL)   AS has_page_number,
  COUNT(*) FILTER (WHERE section_title IS NOT NULL) AS has_section_title
FROM knowledge_chunks;


-- 3. ดู embedding dimension จริงในตาราง
SELECT
  attname                                    AS column_name,
  atttypmod                                  AS type_mod,
  pg_catalog.format_type(atttypid, atttypmod) AS full_type
FROM pg_attribute
WHERE attrelid = 'knowledge_chunks'::regclass
  AND attname  = 'embedding';


-- 4. ดูไฟล์ทั้งหมดและ chunk count ของแต่ละไฟล์
SELECT
  kf.id,
  kf.filename,
  kf.file_type,
  kf.status,
  kf.is_active,
  kf.is_deleted,
  kf.version,
  kf.chunk_count,
  COUNT(kc.id) FILTER (WHERE kc.is_active = true) AS active_chunks_in_db
FROM knowledge_files kf
LEFT JOIN knowledge_chunks kc ON kc.file_id = kf.id
WHERE kf.deleted_at IS NULL
GROUP BY kf.id
ORDER BY kf.uploaded_at DESC;


-- 5. ทดสอบ RPC function ว่า return metadata ครบ
--    (ใส่ embedding ตัวอย่าง 768 มิติ ทั้งหมด 0.0)
SELECT
  id,
  file_id,
  LEFT(content, 80)   AS content_preview,
  is_active,
  chunk_index,
  page_number,
  section_title,
  version,
  similarity,
  metadata->>'file_name' AS file_name
FROM match_knowledge_chunks(
  array_fill(0.0::float, ARRAY[768])::vector(768),
  5
);
