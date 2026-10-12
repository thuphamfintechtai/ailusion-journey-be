from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, LlmClient
from app.services import llm_client

router = APIRouter(prefix="/places", tags=["places"])

# Google place_id ('ChIJ...'), cid (digits) or data_id ('0x..:0x..'): no '/' or '.', so the id
# can never reach another LLM service route.
PLACE_ID = r"^[A-Za-z0-9_:\-]+$"


@router.get("/{place_id}")
async def get_place(
    place_id: Annotated[str, Path(min_length=1, max_length=200, pattern=PLACE_ID)],
    user: CurrentUser,
    llm: LlmClient,
    destination: Annotated[str | None, Query(max_length=100)] = None,
    review_photos: bool = False,
) -> dict[str, Any]:
    """Details of a place card in the itinerary: info, hours, photos and up to 10 reviews.

    The response is the LLM service's `PlaceDetail` unchanged:
    `{place_id, name, category, address, phone, website, maps_url, lat, lng, rating,
    reviews_count, price, hours, description, attributes, images, reviews,
    has_review_photos_option}`.

    `review_photos=true` asks for live Google reviews that carry photos (1 SerpApi call per
    request, cached 10 minutes upstream); default uses stored reviews.
    """
    return await llm_client.get_place(
        llm, place_id=place_id, destination=destination, review_photos=review_photos
    )
