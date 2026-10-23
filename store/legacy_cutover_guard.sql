-- Installed explicitly by the cutover operator after the inventory is reviewed.
-- No legacy row is rewritten. ACCESS EXCLUSIVE lock waits for old DML to finish.
LOCK TABLE public.memories IN ACCESS EXCLUSIVE MODE;
CREATE OR REPLACE FUNCTION public.is_banking_preference_key(p_key text) RETURNS boolean
 LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT lower(replace(replace(btrim(p_key),' ','_'),'-','_')) IN ('preferred_response_language','preferred_response_format')
$$;
CREATE OR REPLACE FUNCTION public.block_banking_preference_write() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF public.is_banking_preference_key(NEW.attribute) THEN
   RAISE EXCEPTION 'Banking preference writer retired' USING ERRCODE='55000'; END IF;
 ELSE
  IF public.is_banking_preference_key(OLD.attribute) OR public.is_banking_preference_key(NEW.attribute) THEN
   -- Permit cleanup and bookkeeping; never permit changed/rehydrated content,
   -- attribution, embedding, or a removed tombstone.
   IF NEW.content IS DISTINCT FROM OLD.content OR NEW.attribute IS DISTINCT FROM OLD.attribute OR
      NEW.source IS DISTINCT FROM OLD.source OR NEW.subject_id IS DISTINCT FROM OLD.subject_id OR
      NEW.actor_id IS DISTINCT FROM OLD.actor_id OR
      to_jsonb(NEW)->'embedding' IS DISTINCT FROM to_jsonb(OLD)->'embedding' OR
      (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL) OR
      (OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS NULL) OR
      NEW.reinforcement_count IS DISTINCT FROM OLD.reinforcement_count OR
      NEW.weight IS DISTINCT FROM OLD.weight THEN
    RAISE EXCEPTION 'Banking preference writer retired' USING ERRCODE='55000'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.block_banking_preference_write() FROM PUBLIC;
DROP TRIGGER IF EXISTS banking_preference_writer_retired ON public.memories;
CREATE TRIGGER banking_preference_writer_retired BEFORE INSERT OR UPDATE ON public.memories
 FOR EACH ROW EXECUTE FUNCTION public.block_banking_preference_write();
