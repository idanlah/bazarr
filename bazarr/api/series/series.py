# coding=utf-8

import operator

from flask_restx import Resource, Namespace, reqparse, fields
from functools import reduce

from app.database import get_exclusion_clause, TableEpisodes, TableShows
from subtitles.indexer.series import list_missing_subtitles, series_scan_subtitles
from subtitles.mass_download import series_download_subtitles
from subtitles.wanted import wanted_search_missing_subtitles_series
from app.event_handler import event_stream
from api.swaggerui import subtitles_model, subtitles_language_model, audio_language_model

from ..utils import authenticate, postprocessSeries, None_Keys


api_ns_series = Namespace('Series', description='List series metadata, update series languages profile or run actions '
                                                'for specific series.')


@api_ns_series.route('series')
class Series(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('seriesid[]', type=int, action='append', required=False, default=[],
                                    help='Series IDs to get metadata for')

    get_subtitles_model = api_ns_series.model('subtitles_model', subtitles_model)
    get_subtitles_language_model = api_ns_series.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_series.model('audio_language_model', audio_language_model)

    data_model = api_ns_series.model('series_data_model', {
        'alternativeTitles': fields.List(fields.String),
        'audio_language': fields.Nested(get_audio_language_model),
        'episodeFileCount': fields.Integer(),
        'episodeMissingCount': fields.Integer(),
        'fanart': fields.String(),
        'imdbId': fields.String(),
        'monitored': fields.Boolean(),
        'overview': fields.String(),
        'path': fields.String(),
        'poster': fields.String(),
        'profileId': fields.Integer(),
        'seriesType': fields.String(),
        'sonarrSeriesId': fields.Integer(),
        'sortTitle': fields.String(),
        'tags': fields.List(fields.String),
        'title': fields.String(),
        'tvdbId': fields.Integer(),
        'year': fields.String(),
    })

    get_response_model = api_ns_series.model('SeriesGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
    })

    @authenticate
    @api_ns_series.marshal_with(get_response_model, code=200)
    @api_ns_series.doc(parser=get_request_parser)
    @api_ns_series.response(200, 'Success')
    @api_ns_series.response(401, 'Not Authenticated')
    def get(self):
        """List series metadata for specific series"""
        args = self.get_request_parser.parse_args()
        start = args.get('start')
        length = args.get('length')
        seriesId = args.get('seriesid[]')

        count = TableShows.select().count()

        if len(seriesId) != 0:
            result = TableShows.select() \
                .where(TableShows.sonarrSeriesId.in_(seriesId)) \
                .order_by(TableShows.sortTitle).dicts()
        else:
            result = TableShows.select().order_by(TableShows.sortTitle).limit(length).offset(start).dicts()

        result = list(result)

        for item in result:
            postprocessSeries(item)

            # Add missing subtitles episode count
            episodes_missing_conditions = [(TableEpisodes.sonarrSeriesId == item['sonarrSeriesId']),
                                           (TableEpisodes.missing_subtitles != '[]')]
            episodes_missing_conditions += get_exclusion_clause('series')

            episodeMissingCount = TableEpisodes.select(TableShows.tags,
                                                       TableEpisodes.monitored,
                                                       TableShows.seriesType) \
                .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId)) \
                .where(reduce(operator.and_, episodes_missing_conditions)) \
                .count()
            item.update({"episodeMissingCount": episodeMissingCount})

            # Add episode count
            episodeFileCount = TableEpisodes.select(TableShows.tags,
                                                    TableEpisodes.monitored,
                                                    TableShows.seriesType) \
                .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId)) \
                .where(TableEpisodes.sonarrSeriesId == item['sonarrSeriesId']) \
                .count()
            item.update({"episodeFileCount": episodeFileCount})

        return {'data': result, 'total': count}

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, action='append', required=False, default=[],
                                     help='Sonarr series ID')
    post_request_parser.add_argument('profileid', type=str, action='append', required=False, default=[],
                                     help='Languages profile(s) ID or "none"')

    @authenticate
    @api_ns_series.doc(parser=post_request_parser)
    @api_ns_series.response(204, 'Success')
    @api_ns_series.response(401, 'Not Authenticated')
    @api_ns_series.response(404, 'Languages profile not found')
    def post(self):
        """Update specific series languages profile"""
        args = self.post_request_parser.parse_args()
        seriesIdList = args.get('seriesid')
        profileIdList = args.get('profileid')

        for idx in range(len(seriesIdList)):
            seriesId = seriesIdList[idx]
            profileId = profileIdList[idx]

            if profileId in None_Keys:
                profileId = None
            else:
                try:
                    profileId = int(profileId)
                except Exception:
                    return 'Languages profile not found', 404

            TableShows.update({
                TableShows.profileId: profileId
            }) \
                .where(TableShows.sonarrSeriesId == seriesId) \
                .execute()

            list_missing_subtitles(no=seriesId, send_event=False)

            event_stream(type='series', payload=seriesId)

            episode_id_list = TableEpisodes \
                .select(TableEpisodes.sonarrEpisodeId) \
                .where(TableEpisodes.sonarrSeriesId == seriesId) \
                .dicts()

            for item in episode_id_list:
                event_stream(type='episode-wanted', payload=item['sonarrEpisodeId'])

        event_stream(type='badges')

        return '', 204

    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('seriesid', type=int, required=False, help='Sonarr series ID')
    patch_request_parser.add_argument('action', type=str, required=False, help='Action to perform from ["scan-disk", '
                                                                               '"search-missing", "search-wanted"]')

    @authenticate
    @api_ns_series.doc(parser=patch_request_parser)
    @api_ns_series.response(204, 'Success')
    @api_ns_series.response(400, 'Unknown action')
    @api_ns_series.response(401, 'Not Authenticated')
    def patch(self):
        """Run actions on specific series"""
        args = self.patch_request_parser.parse_args()
        seriesid = args.get('seriesid')
        action = args.get('action')
        if action == "scan-disk":
            series_scan_subtitles(seriesid)
            return '', 204
        elif action == "search-missing":
            series_download_subtitles(seriesid)
            return '', 204
        elif action == "search-wanted":
            wanted_search_missing_subtitles_series()
            return '', 204

        return 'Unknown action', 400
