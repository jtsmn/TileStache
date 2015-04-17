SELECT
    location.id as __id__,
    allowed,
    ST_Transform(linestring, 900913) as __geometry__

FROM
    ways, location

WHERE
    location_type like 'osm_way' and osm_id=ways.id and (ST_Transform(linestring, 900913) && !bbox!)
