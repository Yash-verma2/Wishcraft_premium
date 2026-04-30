import os
import requests
import base64
import time
import logging

logger = logging.getLogger(__name__)

class MusicClient:
    def __init__(self):
        self.spotify_id = os.environ.get('SPOTIFY_CLIENT_ID')
        self.spotify_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
        self.spotify_token = None
        self.token_expiry = 0

    def _get_spotify_token(self):
        """Get or refresh Spotify access token."""
        if self.spotify_token and time.time() < self.token_expiry:
            return self.spotify_token

        if not self.spotify_id or not self.spotify_secret:
            return None

        try:
            auth_str = f"{self.spotify_id}:{self.spotify_secret}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()

            response = requests.post(
                'https://accounts.spotify.com/api/token',
                headers={'Authorization': f'Basic {b64_auth}'},
                data={'grant_type': 'client_credentials'},
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json()
                self.spotify_token = data['access_token']
                # Token usually lasts 3600s, buffer by 60s
                self.token_expiry = time.time() + data.get('expires_in', 3600) - 60
                return self.spotify_token
        except Exception as e:
            logger.error(f"Spotify Auth Error: {e}")
        
        return None

    def search(self, query):
        """
        Search for music.
        Priority: Spotify -> iTunes (Fallback)
        """
        if not query:
            return []

        # 1. Try Spotify
        token = self._get_spotify_token()
        if token:
            try:
                # Search for tracks
                headers = {'Authorization': f'Bearer {token}'}
                params = {'q': query, 'type': 'track', 'limit': 10}
                res = requests.get('https://api.spotify.com/v1/search', headers=headers, params=params, timeout=5)
                
                if res.status_code == 200:
                    tracks = []
                    for item in res.json().get('tracks', {}).get('items', []):
                        tracks.append({
                            'id': item['id'],
                            'title': item['name'],
                            'artist': item['artists'][0]['name'],
                            'album_art': item['album']['images'][0]['url'] if item['album']['images'] else '',
                            'url': f"https://open.spotify.com/embed/track/{item['id']}",
                            'type': 'spotify'
                        })
                    return tracks
            except Exception as e:
                logger.error(f"Spotify Search Failed: {e}")

        # 2. Fallback to iTunes
        try:
            params = {'term': query, 'media': 'music', 'entity': 'song', 'limit': 10}
            res = requests.get('https://itunes.apple.com/search', params=params, timeout=5)
            
            if res.status_code == 200:
                tracks = []
                for item in res.json().get('results', []):
                    tracks.append({
                        'id': str(item.get('trackId')),
                        'title': item.get('trackName'),
                        'artist': item.get('artistName'),
                        'album_art': item.get('artworkUrl100'),
                        'url': item.get('previewUrl'),
                        'type': 'mp3'
                    })
                return tracks
        except Exception as e:
            logger.error(f"iTunes Search Failed: {e}")

        return []
