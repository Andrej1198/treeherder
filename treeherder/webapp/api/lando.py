import logging

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST

from treeherder.utils.http import make_request
from treeherder.webapp.api.serializers import LandoQuerySerializer

logger = logging.getLogger(__name__)
LANDO_BASE_URL = "https://api.lando.services.mozilla.com"


class LandoViewSet(viewsets.ViewSet):
    @action(detail=False)
    def tocommit(self, request, project):
        query_params = LandoQuerySerializer(data=request.query_params)
        if not query_params.is_valid():
            return Response(data=query_params.errors, status=HTTP_400_BAD_REQUEST)
        newlando = query_params.validated_data["newlando"]
        baselando = query_params.validated_data["baselando"]
        newcommit = make_request(f"{LANDO_BASE_URL}/landing_jobs/{newlando}").json()["commit_id"]
        basecommit = make_request(f"{LANDO_BASE_URL}/landing_jobs/{baselando}").json()["commit_id"]
        query_params.validate_pushes(newcommit, newlando, basecommit, baselando)
        return Response({"baseRevision": newcommit, "newRevision": basecommit})
