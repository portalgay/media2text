-- media2text：为 history / records 表增加「识别来源」列（与 SQLite 迁移行为一致）
-- 已有库执行一次即可；新库若使用 supabase_records.sql 已含本列则无需再执行本脚本。

-- Supabase / PostgreSQL
ALTER TABLE public.records
    ADD COLUMN IF NOT EXISTS recognition_type TEXT NOT NULL DEFAULT 'funasr';

COMMENT ON COLUMN public.recognition_type IS '识别来源：subtitle(内嵌字幕) / funasr / dashscope';
