SELECT
    location.id as __id__,
    allowed,
    ST_Transform(geom, 900913) as __geometry__

FROM
    nodes, location

WHERE
    location_type like 'osm_node' and osm_id=nodes.id and (ST_Transform(geom, 900913) && !bbox!)
