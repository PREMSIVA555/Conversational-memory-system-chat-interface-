-- Closed JSON proposal shapes also apply to temporary response-only proposals.
CREATE FUNCTION banking.valid_preference_proposals(p_proposals jsonb) RETURNS boolean
 LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item jsonb;
BEGIN
 IF jsonb_typeof(p_proposals) IS DISTINCT FROM 'array' OR jsonb_array_length(p_proposals)>2 THEN RETURN false; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(p_proposals) LOOP
  IF (jsonb_typeof(item)='object' AND item ?& ARRAY['key','value','operation','scope_key'] AND
   (SELECT count(*) FROM jsonb_object_keys(item))=4 AND
   item->>'key' IN ('preferred_response_language','preferred_response_format') AND
   (item->>'scope_key'='global' OR item->>'scope_key' ~ '^(conversation|response):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') AND
   ((item->>'operation'='DELETE' AND item->'value'='null'::jsonb) OR
    (item->>'operation'='SET' AND jsonb_typeof(item->'value')='string' AND
     ((item->>'key'='preferred_response_language' AND item->>'value' IN ('en','hi')) OR
      (item->>'key'='preferred_response_format' AND item->>'value' IN ('concise','detailed')))))) IS NOT TRUE
   THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION banking.valid_preference_proposals(jsonb) FROM PUBLIC;
ALTER TABLE banking.preference_proposals ADD CONSTRAINT closed_proposal_shapes CHECK(banking.valid_preference_proposals(proposals));
