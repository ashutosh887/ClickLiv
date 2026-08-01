-- content_id sits last among the dims in minute_occupancy's ORDER BY (D7), so a
-- content_id filter only gets "generic exclusion search" pruning off the base table.
-- A projection reordered by (content_id, minute) makes content_id the leading key,
-- which is a real prefix and gets binary search pruning instead.
ALTER TABLE minute_occupancy MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild';

ALTER TABLE minute_occupancy ADD PROJECTION IF NOT EXISTS proj_content_minute
(
    SELECT * ORDER BY (content_id, minute)
);

ALTER TABLE minute_occupancy MATERIALIZE PROJECTION proj_content_minute SETTINGS mutations_sync = 2;
